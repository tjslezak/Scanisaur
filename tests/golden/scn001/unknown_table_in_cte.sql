WITH recent AS (
  SELECT user_id FROM sessions WHERE event_date >= '2026-09-01'
)
SELECT COUNT(*) AS n FROM recent
