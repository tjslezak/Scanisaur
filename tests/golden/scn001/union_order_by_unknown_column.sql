SELECT user_id FROM events
UNION ALL
SELECT user_id FROM users
ORDER BY usr_id
