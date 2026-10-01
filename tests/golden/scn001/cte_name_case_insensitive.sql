WITH Recent AS (SELECT user_id FROM events WHERE event_date = '2026-09-01')
SELECT user_id FROM recent
