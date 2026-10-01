SELECT user_id, country
FROM events e
JOIN users u ON e.user_id = u.user_id
