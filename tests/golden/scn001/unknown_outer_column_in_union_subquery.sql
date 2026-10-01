SELECT e.user_id
FROM events e
WHERE EXISTS (
  SELECT 1 FROM users u WHERE u.user_id = e.user_id
  UNION ALL
  SELECT 1 FROM users u2 WHERE u2.user_id = e.usr_id
)
