SELECT user_id FROM events
WHERE COALESCE(event_date, DATE(event_ts)) >= '2026-09-01'
