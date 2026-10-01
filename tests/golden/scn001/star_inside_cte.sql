WITH base AS (SELECT * FROM events)
SELECT user_id FROM base WHERE event_date = '2026-09-01'
