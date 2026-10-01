SELECT e.event_nam, u.countri
FROM events e
JOIN users u USING (user_id)
