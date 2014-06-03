import os
import itertools
import gzip
import datetime
import json

from cdf.features.main.streams import IdStreamDef, InfosStreamDef
from cdf.features.main.utils import get_url_to_id_dict_from_stream
from cdf.features.ganalytics.streams import (RawVisitsStreamDef,
                                             VisitsStreamDef)
from cdf.tasks.decorators import TemporaryDirTask as with_temporary_dir
from cdf.utils import s3
from cdf.core.constants import FIRST_PART_ID_SIZE, PART_ID_SIZE
from cdf.core.decorators import feature_enabled

from analytics.import_analytics import import_data

from cdf.utils.auth import get_credentials
from cdf.features.ganalytics.constants import TOP_GHOST_PAGES_NB
from cdf.features.ganalytics.matching import MATCHING_STATUS, get_urlid
from cdf.features.ganalytics.streams import _iterate_sources
from cdf.features.ganalytics.ghost import (update_session_count,
                                           update_top_ghost_pages,
                                           update_ghost_pages_session_count,
                                           save_ghost_pages,
                                           save_ghost_pages_session_count)


@with_temporary_dir
@feature_enabled('ganalytics')
def import_data_from_ganalytics(access_token,
                                refresh_token,
                                ganalytics_site_id,
                                s3_uri,
                                date_start=None,
                                date_end=None,
                                tmp_dir=None,
                                force_fetch=False):
    """
    Request data from google analytics
    TODO (maybe) : take a `refresh_token` instead of an `access_token`
    :param access_token: the access token to retrieve the data from
                         Google Analytics Core Reporting API
    :type access_token: str
    :param refresh_token: the refresh token.
                          The refresh token is used to regenerate an access
                          token when the current one has expired.
    :type refresh_token: str
    :param ganalytics_site_id: the id of the Google Analytics view to retrieve
                               data from.
                               It is an integer with 8 digits.
                               Caution: this is NOT the property id.
                               There may be multiple views for a given property
                               id. (for instance one unfiltered view and one
                               where the traffic from inside the company is
                               filtered).
    :type ganalytics_size_id: int
    :param date_start: Beginning date to retrieve data.
                       If None, the task uses the date from 31 days ago.
                       (so that if both date_start and date_end are None,
                       the import period is the last 30 days)
    :param date_start: date
    :param date_end: Final date to retrieve data.
                     If none, the task uses the date from yesterday.
    :param date_end: date
    :param s3_uri: the uri where to store the data
    :type s3_uri: str
    :param tmp_dir: the path to the tmp directory to use.
                    If None, a new tmp directory will be created.
    :param tmp_dir: str
    :param force_fetch: if True, the files will be downloaded from s3
                        even if they are in the tmp directory.
                        if False, files that are present in the tmp_directory
                        will not be downloaded from s3.
    """

    #set date_start and date_end default values if necessary
    if date_start is None:
        date_start = datetime.date.today() - datetime.timedelta(31)
    if date_end is None:
        date_end = datetime.date.today() - datetime.timedelta(1)
    credentials = get_credentials(access_token, refresh_token)
    import_data(
        "ga:{}".format(ganalytics_site_id),
        credentials,
        date_start,
        date_end,
        tmp_dir
    )
    for f in ['analytics.data.gz', 'analytics.meta.json']:
        s3.push_file(
            os.path.join(s3_uri, f),
            os.path.join(tmp_dir, f)
        )

    metadata = load_analytics_metadata(tmp_dir)
    # Advise the workflow that we need to send data to the remote db
    # through the api by calling a feature endpoint (prefixed by its revision)
    return {
        "api_requests": [
            {
                "method": "patch",
                "endpoint_url": "revision",
                "endpoint_suffix": "ganalytics/",
                "data": {
                    "sample_rate": metadata["sample_rate"],
                    "sample_size": metadata["sample_size"],
                    "sampled": metadata["sampled"],
                    "queries_count": metadata["queries_count"]
                }
            }
        ]
    }


def load_analytics_metadata(tmp_dir):
    """Load the analytics metadata and returns it as a dict.
    This function was introduced to make test writing easier
    :param tmp_dir: the tmp directory used by the task
    :type tmp_dir: str
    """
    return json.loads(open(os.path.join(tmp_dir, 'analytics.meta.json')).read())


@with_temporary_dir
@feature_enabled('ganalytics')
def match_analytics_to_crawl_urls(s3_uri, first_part_id_size=FIRST_PART_ID_SIZE, part_id_size=PART_ID_SIZE,
                                  protocol='http', tmp_dir=None, force_fetch=False):
    """
    :param raw_data_file : the google analytics raw data file
    :pram s3_uri : the root storage uri where to find the given crawl dataset

    Transform a row file like :
    www.site.com/my_url organic google 12
    www.site.com/my_another_url organic google 50

    To :
    576 organic google 12
    165 organic google 50
    """
    id_stream = IdStreamDef.get_stream_from_s3(s3_uri, tmp_dir=tmp_dir)
    info_stream = InfosStreamDef.get_stream_from_s3(s3_uri, tmp_dir=tmp_dir)
    id_idx = InfosStreamDef.field_idx("id")
    http_code_idx = InfosStreamDef.field_idx("http_code")
    urlid_to_http_code = {s[id_idx]: s[http_code_idx] for s in info_stream}

    url_to_id = get_url_to_id_dict_from_stream(id_stream)
    dataset = VisitsStreamDef.create_temporary_dataset()

    #create a gzip file to store ambiguous visits
    #we cannot use stream defs as the entries do not have any urlids
    #(by definition)
    #thus they cannot be split.
    ambiguous_urls_filename = 'ambiguous_urls_dataset.gz'
    ambiguous_urls_filepath = os.path.join(tmp_dir,
                                           ambiguous_urls_filename)

    with gzip.open(ambiguous_urls_filepath, 'wb') as ambiguous_urls_file:

        stream = RawVisitsStreamDef.get_stream_from_s3_path(
            os.path.join(s3_uri, 'analytics.data.gz'),
            tmp_dir=tmp_dir,
            force_fetch=force_fetch
        )

        #init data structures to save the top ghost pages
        #and the number of sessions for ghost pages
        top_ghost_pages = {}
        ghost_pages_session_count = {}
        for medium, source in _iterate_sources():
            medium_source = "{}.{}".format(medium, source)
            top_ghost_pages[medium_source] = []
            ghost_pages_session_count[medium_source] = 0

        #precompute field indexes as it would be too long to compute them
        #inside the loop
        fields_list = ["url", "medium", "source", "social_network", "nb"]
        url_idx, medium_idx, source_idx, social_network_idx, sessions_idx = RawVisitsStreamDef.fields_idx(fields_list)
        #get all the entries corresponding the the same url
        for url_without_protocol, entries in itertools.groupby(stream, lambda x: x[url_idx]):
            url_id, matching_status = get_urlid(url_without_protocol,
                                                url_to_id,
                                                urlid_to_http_code)
            if url_id:
                #if url is in the crawl, add its data to the dataset
                for entry in entries:
                    dataset_entry = list(entry)
                    dataset_entry[0] = url_id
                    dataset.append(*dataset_entry)
                    #store ambiguous url ids
                    if matching_status == MATCHING_STATUS.AMBIGUOUS:
                        line = "\t".join([str(i) for i in entry])
                        line = "{}\n".format(line)
                        line = unicode(line)
                        ambiguous_urls_file.write(line)
            elif matching_status == MATCHING_STATUS.NOT_FOUND:
                #if it is not in the crawl, aggregate the sessions
                #so that you can decide whether or not the url belongs to
                #the top ghost pages and thus either keep the entry
                #or delete to save memory.
                #If you are not sure that you got all the entries for a given
                #url, you can not decide to throw it away, as its number of
                #sessions may be increased by a new entry and
                #it then may become a top ghost page.
                aggregated_session_count = {}
                for entry in entries:
                    medium = entry[medium_idx]
                    source = entry[source_idx]
                    social_network = entry[social_network_idx]
                    nb_sessions = entry[sessions_idx]

                    update_session_count(aggregated_session_count,
                                         medium,
                                         source,
                                         social_network,
                                         nb_sessions)

                #update the top ghost pages for this url
                update_top_ghost_pages(top_ghost_pages,
                                       TOP_GHOST_PAGES_NB,
                                       url_without_protocol,
                                       aggregated_session_count)

                #update the session count
                update_ghost_pages_session_count(ghost_pages_session_count,
                                                 aggregated_session_count)

    #save top ghost pages in dedicated files
    ghost_file_paths = []
    for key, values in top_ghost_pages.iteritems():
        #convert the heap into a sorted list
        values = sorted(values, reverse=True)
        #protocol is missing, we arbitrarly prefix all the urls with http
        values = [(count, "http://{}".format(url)) for count, url in values]
        #create a dedicated file
        crt_ghost_file_path = save_ghost_pages(key, values, tmp_dir)
        ghost_file_paths.append(crt_ghost_file_path)

    #push top ghost files to s3
    for ghost_file_path in ghost_file_paths:
        s3.push_file(
            os.path.join(s3_uri, os.path.basename(ghost_file_path)),
            ghost_file_path
        )

    #save session counts for ghost pages
    session_count_path = save_ghost_pages_session_count(
        ghost_pages_session_count,
        tmp_dir)
    s3.push_file(
        os.path.join(s3_uri, os.path.basename(session_count_path)),
        session_count_path
    )

    s3.push_file(
        os.path.join(s3_uri, ambiguous_urls_filename),
        ambiguous_urls_filepath
    )
    dataset.persist_to_s3(s3_uri,
                          first_part_id_size=first_part_id_size,
                          part_id_size=part_id_size)