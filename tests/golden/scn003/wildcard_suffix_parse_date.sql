SELECT event_name FROM `proj.ga4.events_*`
WHERE PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) >= '2021-01-01'
