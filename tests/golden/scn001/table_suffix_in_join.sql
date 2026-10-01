SELECT g.event_name, u.country
FROM `proj.ga4.events_*` g
JOIN users u ON g.user_pseudo_id = u.user_id
WHERE _TABLE_SUFFIX = '20260901'
