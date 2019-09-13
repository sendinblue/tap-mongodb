#!/usr/bin/env python3
import copy
import time
from bson import objectid
import pymongo
import singer
from singer import metadata, utils
import tap_mongodb.sync_strategies.common as common

LOGGER = singer.get_logger()

# This code has to change somehow
def get_max_id_value(collection):
    row = collection.find_one(sort=[("_id", pymongo.DESCENDING)])
    id_value = row['_id']
    id_type = id_value.__class__.__name__
    # Should this just return id_value?
    #return common.string_to_class(st
    return id_value

# pylint: disable=too-many-locals,invalid-name
def sync_collection(client, stream, state, projection):
    tap_stream_id = stream['tap_stream_id']
    LOGGER.info('Starting full table sync for %s', tap_stream_id)

    mdata = metadata.to_map(stream['metadata'])
    stream_metadata = mdata.get(())
    database_name = stream_metadata['database-name']

    db = client[database_name]
    collection = db[stream['stream']]

    #before writing the table version to state, check if we had one to begin with
    first_run = singer.get_bookmark(state, stream['tap_stream_id'], 'version') is None

    # last run was interrupted if there is a last_id_fetched bookmark
    was_interrupted = singer.get_bookmark(state,
                                          stream['tap_stream_id'],
                                          'last_id_fetched') is not None

    #pick a new table version if last run wasn't interrupted
    if was_interrupted:
        stream_version = singer.get_bookmark(state, stream['tap_stream_id'], 'version')
    else:
        stream_version = int(time.time() * 1000)

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'version',
                                  stream_version)
    singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

    activate_version_message = singer.ActivateVersionMessage(
        stream=common.calculate_destination_stream_name(stream),
        version=stream_version
    )


    # For the initial replication, emit an ACTIVATE_VERSION message
    # at the beginning so the records show up right away.
    if first_run:
        singer.write_message(activate_version_message)

    # retrieving the bookmark needs to come back as a class too. common.string_to_class
    if singer.get_bookmark(state, stream['tap_stream_id'], 'max_id_value'):
        # There is a bookmark
        max_id_value = singer.get_bookmark(state, stream['tap_stream_id'], 'max_id_value')
        max_id_type = singer.get_bookmark(state, stream['tap_stream_id'], 'max_id_type')
        max_id_value = common.string_to_class(max_id_value, max_id_type)
    else:
        max_id_value = get_max_id_value(collection)

    # this might need to change too.
    last_id_fetched = singer.get_bookmark(state,
                                          stream['tap_stream_id'],
                                          'last_id_fetched')

    # also write the type
    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'max_id_value',
                                  max_id_value)
    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'max_id_type',
                                  max_id_value.__class__.__name__)


    # replace this with class
    find_filter = {'$lte': max_id_value}
    if last_id_fetched:
        # replace this with class
        find_filter['$gte'] = objectid.ObjectId(last_id_fetched)

    query_message = 'Querying {} with:\n\tFind Parameters: {}'.format(
        stream['tap_stream_id'],
        find_filter)
    if projection:
        query_message += '\n\tProjection: {}'.format(projection)
    # pylint: disable=logging-format-interpolation
    LOGGER.info(query_message)


    with collection.find({'_id': find_filter},
                         projection,
                         sort=[("_id", pymongo.ASCENDING)]) as cursor:
        rows_saved = 0
        time_extracted = utils.now()
        start_time = time.time()

        for row in cursor:
            rows_saved += 1

            record_message = common.row_to_singer_record(stream,
                                                         row,
                                                         stream_version,
                                                         time_extracted)

            singer.write_message(record_message)

            state = singer.write_bookmark(state,
                                          stream['tap_stream_id'],
                                          'last_id_fetched',
                                          str(row['_id']))
            state = singer.write_bookmark(state,
                                          stream['tap_stream_id'],
                                          'last_id_fetched_type',
                                          row['_id'].__class__.__name__)

            if rows_saved % common.UPDATE_BOOKMARK_PERIOD == 0:
                singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

        common.COUNTS[tap_stream_id] += rows_saved
        common.TIMES[tap_stream_id] += time.time()-start_time

    # clear max pk value and last pk fetched upon successful sync
    singer.clear_bookmark(state, stream['tap_stream_id'], 'max_id_value')
    singer.clear_bookmark(state, stream['tap_stream_id'], 'max_id_type')
    singer.clear_bookmark(state, stream['tap_stream_id'], 'last_id_fetched')
    singer.clear_bookmark(state, stream['tap_stream_id'], 'last_id_fetched_type')

    state = singer.write_bookmark(state,
                                  stream['tap_stream_id'],
                                  'initial_full_table_complete',
                                  True)

    singer.write_message(activate_version_message)

    LOGGER.info('Syncd {} records for {}'.format(rows_saved, tap_stream_id))
