SELECT user_id FROM users AS u
WHERE EXISTS (SELECT * FROM events AS e WHERE e.user_id = u.user_id AND e.event_date = '2026-09-30')
