SELECT * FROM events WHERE event_date = '2026-09-30'
UNION ALL
SELECT * FROM events WHERE event_date = '2026-09-29'
LIMIT 10
