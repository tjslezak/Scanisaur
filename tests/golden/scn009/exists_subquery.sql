SELECT u.user_id FROM users AS u
WHERE EXISTS (
  SELECT 1 FROM Orders AS o WHERE o.user_id = u.user_id AND o.order_date = '2026-09-30'
)
