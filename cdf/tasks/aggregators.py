# -*- coding: utf-8 -*-
import os
import json
import csv
from urlparse import urlparse

from pandas import HDFStore, Index

from cdf.exceptions import MissingResource
from cdf.utils.loading import build_dataframe_from_csv
from cdf.utils.s3 import fetch_file, fetch_files, push_file, push_content
from cdf.utils.path import makedirs
from cdf.metadata.aggregates.aggregates_metadata import CROSS_PROPERTIES_COLUMNS
from cdf.analysis.suggestions.aggregator import MetricsAggregator, MetricsConsolidator
from cdf.analysis.urls.generators.suggestions import MetadataClusterMixin
from cdf.analysis.urls.constants import SUGGEST_CLUSTERS
from cdf.analysis.suggestions.query import MetricsQuery, SuggestQuery
from cdf.analysis.urls.utils import merge_queries_filters

from .decorators import TemporaryDirTask as with_temporary_dir
from .constants import DEFAULT_FORCE_FETCH

from cdf.analysis.urls.query_helpers import (
    get_filters_from_http_code_range,
    get_filters_from_agg_canonical_field
)

from cdf.features.main.streams import IdStreamDef, InfosStreamDef, SuggestStreamDef
from cdf.features.links.streams import (
    OutlinksCountersStreamDef, OutcanonicalCountersStreamDef, OutredirectCountersStreamDef,
    InlinksCountersStreamDef, IncanonicalCountersStreamDef, InredirectCountersStreamDef,
    BadLinksCountersStreamDef
)
from cdf.features.semantic_metadata.streams import ContentsDuplicateStreamDef


@with_temporary_dir
def compute_aggregators_from_part_id(crawl_id, s3_uri, part_id, tmp_dir=None, force_fetch=DEFAULT_FORCE_FETCH):
    suggest_dir_path = os.path.join(tmp_dir, 'suggest')
    makedirs(suggest_dir_path, exist_ok=True)

    streams_def = [
        IdStreamDef, InfosStreamDef,
        OutlinksCountersStreamDef, OutcanonicalCountersStreamDef, OutredirectCountersStreamDef,
        InlinksCountersStreamDef, IncanonicalCountersStreamDef, InredirectCountersStreamDef,
        SuggestStreamDef, ContentsDuplicateStreamDef, BadLinksCountersStreamDef
    ]
    streams = []
    for s in streams_def:
        streams.append(s.get_stream_from_s3(s3_uri, part_id=part_id, tmp_dir=tmp_dir, force_fetch=force_fetch))

    aggregator = MetricsAggregator(streams)
    content = json.dumps(aggregator.get())
    f = open(os.path.join(tmp_dir, 'suggest/counters.{}.json'.format(part_id)), 'w')
    f.write(content)
    f.close()
    push_file(
        os.path.join(s3_uri, 'suggest/counters.{}.json'.format(part_id)),
        os.path.join(tmp_dir, 'suggest/counters.{}.json'.format(part_id))
    )


@with_temporary_dir
def consolidate_aggregators(crawl_id, s3_uri, tmp_dir=None, force_fetch=False):
    """
    Fetch all part_id's aggregators and merge them
    """
    makedirs(tmp_dir, exist_ok=True)

    # Fetch hdf5 file that already contains the full list of requests
    h5_file = os.path.join(tmp_dir, 'suggest.h5')
    if os.path.exists(h5_file):
        os.remove(h5_file)

    # new
    u = MetadataClusterMixin()
    for cluster_type in SUGGEST_CLUSTERS:
        filename = 'clusters_{}.tsv'.format(cluster_type)

        source_uri = os.path.join(s3_uri, filename)
        destination_path = os.path.join(tmp_dir, filename)

        file_, fetched = fetch_file(source_uri,
                                    destination_path,
                                    force_fetch=force_fetch)

        cluster_values = []
        csv_reader = csv.reader(open(file_),
                                delimiter="\t",
                                quotechar=None,
                                quoting=csv.QUOTE_NONE)
        for row in csv_reader:
            pattern, verbose_pattern, hash, _ = row
            cluster_values.append((pattern, verbose_pattern, hash))
        u.add_pattern_cluster(cluster_type, cluster_values)

    store = HDFStore(h5_file, complevel=9, complib='blosc')
    # Make K/V Store dataframe (hash to request)
    store['requests'] = u.make_clusters_dataframe()

    #fetch child relationship tsv
    children_filename = "cluster_mixed_children.tsv"
    source_uri = os.path.join(s3_uri, children_filename)
    destination_path = os.path.join(tmp_dir, children_filename)
    _f, fetched = fetch_file(source_uri,
                             destination_path,
                             force_fetch=force_fetch)

    #build child relationship dataframe
    child_frame = build_dataframe_from_csv(open(_f, "rb"), ["parent", "child"])
    if len(child_frame) > 0:
        #store dataframe in hdfstore.
        #we do not store empty dataframe in hdfstore since recovering it
        #afterwards raises an exception :
        #ValueError: Shape of passed values is (2, 0), indices imply (2, 1)
        store['children'] = child_frame

    files_fetched = fetch_files(s3_uri,
                                tmp_dir,
                                regexp=['suggest/counters.([0-9]+).json'],
                                force_fetch=force_fetch)

    counters = [json.load(open(path_local)) for path_local, fetched in files_fetched]
    c = MetricsConsolidator(counters)
    df_counter = c.get_dataframe()
    store["full_crawl"] = df_counter[df_counter['query'] == '0'].groupby(CROSS_PROPERTIES_COLUMNS).agg('sum').reset_index()
    suggest_frame = df_counter[df_counter['query'] != '0'].groupby(CROSS_PROPERTIES_COLUMNS).agg('sum').reset_index()
    if len(suggest_frame) == 0:
        #for an unknown reason, pandas raises a :
        #ValueError: Shape of passed values is (75, 0), indices imply (75, 1)
        #
        #when retrieving a dataframe with an empty int64 index.
        #To avoid this, we change the type of the index.
        #It does not hurt, since the dataframe is empty.
        suggest_frame.index = Index([], dtype=int)
    store["suggest"] = suggest_frame

    store.close()
    push_file(os.path.join(s3_uri, 'suggest.h5'), h5_file)


class SuggestSummaryRegister(object):

    def __init__(self, crawl_id, s3_uri, es_location, es_index, es_doc_type, revision_number, tmp_dir, force_fetch=False):
        self.crawl_id = crawl_id
        self.s3_uri = s3_uri
        self.es_location = es_location
        self.es_index = es_index
        self.es_doc_type = es_doc_type
        self.revision_number = revision_number
        self.tmp_dir = tmp_dir
        self.force_fetch = force_fetch
        self._called = False
        self._queue = []
        self._scores = {}

    def register(self, identifier, query, urls_fields=[], urls_filters=[], urls_sort=None):
        self._queue.append([identifier, query, urls_fields, urls_filters, urls_sort])

    def _compute_item(self, suggest_query_object, identifier, query, urls_fields=[], urls_filters=[], urls_sort=None):
        """
        Compute a specific item for the suggested patterns queue
        """
        results = suggest_query_object.query(query)
        for k, result in enumerate(results):
            if result["score"] == 0:
                continue

            hash_id_filters = {'field': 'patterns', 'value': result['query_hash_id'], 'predicate': 'any.eq'}
            result["urls_query_bgn"] = {
                "fields": ["url"] + urls_fields,
                "filters": merge_queries_filters(hash_id_filters, urls_filters)
            }
            result["urls_query"] = {
                "fields": ["url"] + urls_fields,
                "filters": merge_queries_filters(result["query"], urls_filters)
            }

        # Write suggestion file
        summary_file = os.path.join(self.tmp_dir, 'flat_v1.1', 'metrics', 'suggest', '{}.json'.format(identifier))
        makedirs(os.path.join(os.path.dirname(summary_file)), exist_ok=True)
        f = open(os.path.join(summary_file), 'w')
        f.write(json.dumps(results, indent=4))
        f.close()
        push_file(
            os.path.join(self.s3_uri, 'flat_v1.1', 'metrics', 'suggest', '{}.json'.format(identifier)),
            summary_file
        )
        return len(results)

    def _push_summary_scores(self):
        if not self._called:
            raise Exception('Summary has not be called yet')
        summary_file = os.path.join(self.tmp_dir, 'flat_v1.1', 'metrics', 'suggest', 'index.json')
        f = open(os.path.join(summary_file), 'w')
        f.write(json.dumps(self._scores, indent=4))
        f.close()
        push_file(
            os.path.join(self.s3_uri, 'flat_v1.1', 'metrics', 'suggest', 'index.json'),
            summary_file
        )

    def run(self):
        """
        Run all suggested patterns registered
        """
        if not self._called:
            q = SuggestQuery.from_s3_uri(self.crawl_id, self.s3_uri, tmp_dir=self.tmp_dir, force_fetch=self.force_fetch)
            for params in self._queue:
                self._scores[params[0]] = self._compute_item(q, *params)
            self._called = True
            self._push_summary_scores()


def make_counter_file_from_query(crawl_id, s3_uri, revision_number, tmp_dir, identifier, query):
    q = MetricsQuery.from_s3_uri(crawl_id, s3_uri, tmp_dir=tmp_dir)

    is_batch = isinstance(query, list)
    if is_batch:
        identifiers = [k[0] for k in query]
        query = [k[1] for k in query]
    results = q.query(query)

    # If it is a batch query, replace result list by a dictionnary (mapped to query identifiers)
    if is_batch:
        results = {identifier: result for identifier, result in zip(identifiers, results)}

     # Write suggestion file
    summary_file = os.path.join(tmp_dir, 'flat_v1.1', 'metrics', '{}.json'.format(identifier))
    makedirs(os.path.join(os.path.dirname(summary_file)), exist_ok=True)
    f = open(os.path.join(summary_file), 'w')
    f.write(json.dumps(results, indent=4))
    f.close()
    push_file(
        os.path.join(s3_uri, 'flat_v1.1', 'metrics', '{}.json'.format(identifier)),
        summary_file
    )


@with_temporary_dir
def make_suggest_summary_file(crawl_id, s3_uri, es_location, es_index, es_doc_type, revision_number, tmp_dir=None, force_fetch=False):
    counter_kwargs = {
        'crawl_id': crawl_id,
        's3_uri': s3_uri,
        'revision_number': revision_number,
        'tmp_dir': tmp_dir,
    }

    suggest = SuggestSummaryRegister(crawl_id, s3_uri,
                                     es_location, es_index, es_doc_type, revision_number,
                                     tmp_dir=tmp_dir, force_fetch=force_fetch)

    # Full picture
    make_counter_file_from_query(
        identifier='full_picture',
        query=[
            ['global', {}],
            ['http_code', {"group_by": ["http_code"]}],
            ['noindex', {"fields": ["pages_nb"], "group_by": ["index"]}],
            ['nofollow', {"fields": ["pages_nb"], "group_by": ["follow"]}],
            ['content_type', {"fields": ["pages_nb"], "group_by": ["content_type"]}],
            ['depth', {"fields": ["pages_nb"], "group_by": ["depth"]}],
            ['2xx_html', {
                "filters": {
                    "and": [
                        {"field": "content_type", "value": "text/html"},
                        {"field": "http_code", "value": 200, "predicate": "gte"},
                        {"field": "http_code", "value": 299, "predicate": "lte"}
                    ]
                }
            }],
            ['canonical', {
                "fields": ["pages_nb", "canonical_nb"],
                "filters": {
                    "and": [
                        {"field": "content_type", "value": "text/html"},
                        {"field": "http_code", "value": 200, "predicate": "gte"},
                        {"field": "http_code", "value": 299, "predicate": "lte"}
                    ]
                }
            }]
        ],
        **counter_kwargs
    )

    # Http codes by range
    for http_code in (200, 300, 400, 500):
        query = {
            "fields": ["pages_nb"],
            "target_field": "pages_nb",
            "filters": {
                'and': [
                    {"field": "http_code", "value": http_code, "predicate": "gte"},
                    {"field": "http_code", "value": http_code + 99, "predicate": "lt"},
                ]
            }
        }
        if http_code == 300:
            urls_fields = ["redirect.to.url"]
        else:
            urls_fields = ["http_code"]
        urls_filters = get_filters_from_http_code_range(http_code)
        suggest.register(identifier='http_code/{}'.format(str(http_code)[0] + 'xx'), query=query, urls_filters=urls_filters, urls_fields=urls_fields)

    # Incoming redirections
    query = {
        "fields": ["redirects_from_nb"],
        "target_field": "redirects_from_nb",
    }
    urls_fields = ["redirect.from.nb", "redirect.from.urls"]
    urls_filters = {
        "field": "redirect.from.nb",
        "value": 0,
        "predicate": "gt"
    }
    urls_sort = [{"redirects.from.nb": "desc"}]
    suggest.register(identifier='http_code/incoming_redirects', query=query, urls_filters=urls_filters, urls_fields=urls_fields, urls_sort=urls_sort)

    # Metadata types
    for metadata_type in ('title', 'description', 'h1'):
        for metadata_status in ('duplicate', 'not_filled', 'unique'):
            query = {
                "fields": ["pages_nb", "metadata_nb.{}".format(metadata_type), "metadata_duplicate_nb.{}".format(metadata_type)],
                "filters": {"and": [
                    {"field": "content_type", "value": "text/html"},
                    {"field": "http_code", "value": 200, "predicate": "gte"},
                    {"field": "http_code", "value": 300, "predicate": "lt"},
                ]},
                "target_field": "metadata_nb.{}.{}".format(metadata_type, metadata_status)
            }
            if metadata_status == "duplicate":
                urls_fields = [
                    "metadata.{}.contents".format(metadata_type),
                    "metadata.{}.duplicates".format(metadata_type)
                ]
                urls_filters = {"field": "metadata.{}.duplicates.nb".format(metadata_type), "value": 1, "predicate": "gt"}
            elif metadata_status == "unique":
                urls_fields = ["metadata.{}.contents".format(metadata_type)]
                urls_filters = {"and": [
                    {"field": "metadata.{}.nb".format(metadata_type), "value": 1},
                    {"field": "metadata.{}.duplicates.nb".format(metadata_type), "value": 0},
                    {"field": "http_code", "value": [200, 299], "predicate": "between"},
                    {"field": "content_type", "value": "text/html"},
                ]}
            elif metadata_status == "not_filled":
                #metadata is not really "not_filled" for pages other than 2XX
                #and for which content_type is not text/html
                query["filters"] = {
                    "and": [
                        {"field": "content_type", "value": "text/html"},
                        {"field": "http_code", "value": 200, "predicate": "gte"},
                        {"field": "http_code", "value": 299, "predicate": "lte"},
                    ]
                }

                urls_fields = []
                urls_filters = {
                    "and": [
                        {"field": "metadata.{}.nb".format(metadata_type), "value": 0},
                        {"field": "http_code", "value": [200, 299], "predicate": "between"},
                        {"field": "content_type", "value": "text/html"}
                    ]
                }
            else:
                raise Exception("{} must handle urls_fields and urls_filters".format(metadata_status))
            suggest.register(
                identifier='metadata/{}/{}'.format(metadata_type, metadata_status),
                query=query,
                urls_filters=urls_filters,
                urls_fields=urls_fields
            )

    # Speed
    for sort in ('asc', 'desc'):
        urls_sort = [{"delay_last_byte": sort}]
        urls_fields = ["delay_last_byte"]
        query = {
            "fields": ["total_delay_ms", "pages_nb", "delay_lt_500ms", "delay_from_1s_to_2s", "delay_from_500ms_to_1s", "delay_gte_2s"],
            "target_field": {"div": ["total_delay_ms", "pages_nb"]},
            "target_sort": sort,
        }
        sort_verbose = "fastest" if sort == "asc" else "slowest"
        suggest.register(identifier='delay/{}'.format(sort_verbose), query=query, urls_filters=[], urls_fields=urls_fields, urls_sort=urls_sort)

    # Canonicals
    for field in ('filled', 'not_filled', 'equal', 'not_equal', 'incoming'):
        full_field = "canonical_nb.{}".format(field)
        query = {
            "fields": [full_field],
            "target_field": full_field
        }
        if field == "not_filled":
            query["filters"] = {
                "and": [
                    {"field": "content_type", "value": "text/html"},
                    {"field": "http_code", "value": 200, "predicate": "gte"},
                    {"field": "http_code", "value": 299, "predicate": "lte"},
                ]
            }
        if field == "incoming":
            urls_fields = ["canonical.from"]
        else:
            urls_fields = ["canonical.to"]
        urls_filters = get_filters_from_agg_canonical_field(field)
        suggest.register(identifier='canonical/{}'.format(field), query=query, urls_filters=urls_filters, urls_fields=urls_fields)

    # Deeper depths
    for depth in (3, 5, 7, 10):
        query = {
            "fields": ["pages_nb"],
            "target_field": "pages_nb",
            "filters": {
                "field": "depth",
                "value": depth,
                "predicate": "gte"
            }
        }
        urls_fields = ["depth"]
        urls_filters = {
            "field": "depth",
            "value": depth,
            "predicate": "gte"
        }
        suggest.register(identifier='distribution/depth_gte_{}'.format(depth), query=query, urls_filters=urls_filters, urls_fields=urls_fields)

    # no-index urls
    full_field = "index"
    query = {
        "fields": ["pages_nb"],
        "filters": {
            "field": "index", "value": False
        },
        "target_field": "pages_nb"
    }
    urls_fields = ["url"]
    urls_filters = {"field": "metadata.robots.noindex", "value": True}
    suggest.register(identifier='distribution/noindex', query=query, urls_filters=urls_filters, urls_fields=urls_fields)

    # map aggregation fields to urls fields
    fields_mapping = {
        'total': 'total',
        'follow': 'follow.total',
        'follow_unique': 'follow.unique',
        'nofollow': 'nofollow.total',
    }

    # internal/external outlinks
    for status in ('internal', 'external'):
        for sort in ('asc', 'desc'):
            if status == "internal":
                fields_mapping['follow_unique'] = 'follow.unique'
            for field_agg in ["total", "follow", "nofollow"]:
                field_url = fields_mapping[field_agg]
                full_field = "outlinks_{}_nb.{}".format(status, field_agg)
                query = {
                    "fields": ["score", full_field, "pages_nb"],
                    "target_field": {"div": [full_field, "pages_nb"]},
                    "target_sort": sort,
                    "filters": {"field": full_field, "value": 0, "predicate": "gt"}
                }
                urls_full_field = "outlinks_{}.nb.{}".format(status, field_url)
                urls_fields = [urls_full_field]
                urls_filters = {"field": urls_full_field, "value": 0, "predicate": "gt"}
                sort_verbose = "top" if sort == "desc" else "lowest"
                suggest.register(identifier='outlinks_{}/{}_{}'.format(status, sort_verbose, field_agg), query=query, urls_filters=urls_filters, urls_fields=urls_fields)

    # inlinks
    for field_agg in ('total', 'follow', 'follow_unique', 'nofollow'):
        for sort in ('asc', 'desc'):
            field_url = fields_mapping[field_agg]
            full_field = "inlinks_internal_nb.{}".format(field_agg)
            query = {
                "fields": ["score", full_field, "pages_nb"],
                "target_field": {"div": [full_field, "pages_nb"]},
                "target_sort": sort,
                "filters": {"field": full_field, "value": 0, "predicate": "gt"}
            }

            urls_full_field = "inlinks_internal.nb.{}".format(field_url)
            urls_fields = [urls_full_field, "pages_nb"]
            urls_filters = {"field": urls_full_field, "value": 0, "predicate": "gt"}

            sort_verbose = "top" if sort == "desc" else "lowest"
            suggest.register(identifier='inlinks_internal/{}_{}'.format(sort_verbose, field_agg), query=query, urls_filters=urls_filters, urls_fields=urls_fields)

    # Only 1 follow link
    full_field = "inlinks_internal_nb.follow_distribution_urls.1"
    query = {
        "fields": [full_field, "pages_nb"],
        "target_field": full_field
    }
    urls_fields = ["url"]
    urls_filters = {"field": "inlinks_internal.nb.follow.unique", "value": 1}
    suggest.register(identifier='inlinks_internal/1_follow_link', query=query, urls_filters=urls_filters, urls_fields=urls_fields)

    # broken outlinks
    for field in ('any', '3xx', '4xx', '5xx'):
        full_field = "error_links.{}".format(field)
        query = {
            "target_field": full_field
        }
        urls_fields = ["outlinks_errors.{}".format(field)]
        urls_filters = {"field": "outlinks_errors.{}.nb".format(field), "value": 0, "predicate": "gt"}
        urls_sort = [{"outlinks_errors.{}.nb".format(field): "desc"}]
        suggest.register(identifier='outlinks_internal/errors_links_{}'.format(field), query=query, urls_filters=urls_filters, urls_fields=urls_fields, urls_sort=urls_sort)

    suggest.run()


@with_temporary_dir
def update_migration_es_v1_documents(crawl_id, s3_uri, tmp_dir=None, force_fetch=False):
    s3_parse = urlparse(s3_uri)
    push_content(
        os.path.join("s3://{}".format(s3_parse.netloc), "migration_es_v1", "documents_{}".format(crawl_id)),
        "done"
    )


@with_temporary_dir
def update_migration_es_v1_push(crawl_id, s3_uri, tmp_dir=None, force_fetch=False):
    s3_parse = urlparse(s3_uri)
    push_content(
        os.path.join("s3://{}".format(s3_parse.netloc), "migration_es_v1", "push_{}".format(crawl_id)),
        "done"
    )
