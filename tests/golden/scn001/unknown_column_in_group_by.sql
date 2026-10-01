SELECT COUNT(*) AS n
FROM events
WHERE event_date = '2026-09-01'
GROUP BY device_type
