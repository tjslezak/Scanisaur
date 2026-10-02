SELECT event_name, COUNT(*) AS n FROM (SELECT * FROM events WHERE event_date = '2026-09-30') GROUP BY event_name LIMIT 10
