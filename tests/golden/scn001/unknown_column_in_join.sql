SELECT e.user_id, u.country
FROM events e
JOIN users u ON e.user_id = u.id
