SELECT a.event_name
FROM ga4.`events_*` a
JOIN ga4.`events_*` b USING (user_pseudo_id)
WHERE _TABLE_SUFFIX = '20260901'
