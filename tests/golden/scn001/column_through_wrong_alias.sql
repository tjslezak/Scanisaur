SELECT e.country
FROM events e
JOIN users u USING (user_id)
