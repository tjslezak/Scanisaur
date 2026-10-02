SELECT event_name FROM `proj.ga4.events_*`
WHERE _TABLE_SUFFIX = (SELECT MAX(_TABLE_SUFFIX) FROM `proj.ga4.events_*`)
