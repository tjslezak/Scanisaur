-- The GA4 trap: filtering a timestamp column instead of _TABLE_SUFFIX.
SELECT COUNT(*) FROM `proj.ga4.events_*`
WHERE event_name = 'purchase' AND event_timestamp > 1609459200000000
