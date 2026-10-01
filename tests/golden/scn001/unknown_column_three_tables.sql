SELECT plan
FROM events e
JOIN users u USING (user_id)
JOIN Orders o USING (user_id)
