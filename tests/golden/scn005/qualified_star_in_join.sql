SELECT e.*, u.country
FROM events AS e
JOIN users AS u USING (user_id)
WHERE e.event_date = '2026-09-30'
