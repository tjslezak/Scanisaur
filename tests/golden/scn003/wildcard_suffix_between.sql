SELECT COUNT(*) FROM `proj.ga4.events_*`
WHERE _TABLE_SUFFIX BETWEEN '20210101' AND '20210107' AND event_name = 'purchase'
