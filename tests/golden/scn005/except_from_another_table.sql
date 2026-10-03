SELECT * EXCEPT (country)
FROM events
JOIN users USING (user_id)
WHERE event_date = '2026-09-30'
