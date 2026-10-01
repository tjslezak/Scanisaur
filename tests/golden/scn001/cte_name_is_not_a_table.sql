WITH recent AS (
  SELECT user_id, event_name FROM events WHERE event_date >= '2026-09-01'
)
SELECT event_name, COUNT(*) AS n FROM recent GROUP BY event_name
