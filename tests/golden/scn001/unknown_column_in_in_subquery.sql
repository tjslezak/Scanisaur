SELECT user_id
FROM events
WHERE user_id IN (SELECT user_id FROM users WHERE contry = 'US')
