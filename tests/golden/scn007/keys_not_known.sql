SELECT COUNT(u.user_id) AS active
FROM users AS u
JOIN events AS e ON e.user_id = u.user_id
WHERE e.event_date = '2026-09-30'
