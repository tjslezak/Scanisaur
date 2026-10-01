SELECT u.user_id
FROM users u
WHERE EXISTS (
  SELECT 1 FROM events e WHERE e.user_id = u.user_id AND country = 'US'
)
