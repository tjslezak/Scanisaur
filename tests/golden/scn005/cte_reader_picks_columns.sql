WITH recent AS (
  SELECT * FROM events WHERE event_date = '2026-09-30'
)
SELECT user_id, event_name FROM recent LIMIT 5
