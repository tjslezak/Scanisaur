-- `*` reaches the result, but the reader takes one field of `device`, so BigQuery
-- doesn't read every column (#24).
WITH b AS (SELECT * FROM events WHERE event_date = '2026-09-30')
SELECT event_date, event_ts, user_id, event_name, params, device.category FROM b
