SELECT event_name AS name, COUNT(*) AS n
FROM events
WHERE event_date = '2026-09-01'
GROUP BY name
ORDER BY n DESC
