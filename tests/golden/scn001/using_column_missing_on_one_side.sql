SELECT e.user_id
FROM events e
JOIN users u USING (event_name)
