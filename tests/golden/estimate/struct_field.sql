-- BigQuery bills the struct field a query reads, not the whole struct (#22).
SELECT device.category, COUNT(*) AS n FROM events
WHERE event_date = '2026-09-30'
GROUP BY 1
