WITH recent AS (SELECT * FROM events)
SELECT user_id FROM recent WHERE event_date = '2026-09-01'
