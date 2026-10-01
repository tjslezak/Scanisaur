SELECT e.user_id, u.country
FROM events e
JOIN users u ON e.user_id = u.user_id
WHERE e.event_date = '2026-09-01'
