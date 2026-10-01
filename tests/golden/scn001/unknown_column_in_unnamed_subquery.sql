SELECT *
FROM (SELECT user_id FROM events)
WHERE plan = 'pro'
