SELECT user_id
FROM events e
JOIN (SELECT user_id, country FROM users) t ON e.user_id = t.user_id
