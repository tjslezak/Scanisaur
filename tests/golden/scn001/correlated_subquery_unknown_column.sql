SELECT e.user_id
FROM events e
WHERE EXISTS (
  SELECT 1 FROM users u WHERE u.user_id = e.user_id AND u.plan = 'pro'
)
