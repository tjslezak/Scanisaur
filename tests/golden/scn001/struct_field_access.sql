SELECT e.device.category, COUNT(*) AS n
FROM events e
WHERE e.event_date = '2026-09-01'
GROUP BY 1
