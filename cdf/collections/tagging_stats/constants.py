from cdf.streams.constants import CONTENT_TYPE_INDEX

CROSS_PROPERTIES_COLUMNS = ('host', 'resource_type', 'content_type', 'depth', 'http_code', 'index', 'follow')

COUNTERS_FIELDS = (
    'pages_nb',
    'total_delay_ms',
    'redirections_nb',
    'canonical_filled_nb',
    'canonical_duplicates_nb',
    'canonical_incoming_nb',
    'inlinks_nb',
    'inlinks_follow_nb',
    'inlinks_link_nofollow_nb',
    'inlinks_meta_nofollow_nb',
    'inlinks_robots_nofollow_nb',
    'inlinks_config_nofollow_nb',
    'outlinks_nb',
    'outlinks_follow_nb',
    'outlinks_link_nofollow_nb',
    'outlinks_meta_nofollow_nb',
    'outlinks_robots_nofollow_nb',
    'outlinks_config_nofollow_nb',
    'delay_gte_2s',
    'delay_from_1s_to_2s',
    'delay_from_500ms_to_1s',
    'delay_lt_500ms'
)

CROSS_PROPERTIES_META_COLUMNS = ('host', 'resource_type')

META_FIELDS = []
for ct_txt in CONTENT_TYPE_INDEX.itervalues():
    META_FIELDS += ['%s_filled_nb' % ct_txt, '%s_local_unik_nb' % ct_txt, '%s_global_unik_nb' % ct_txt]