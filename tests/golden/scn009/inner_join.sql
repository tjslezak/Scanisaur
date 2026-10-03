SELECT u.user_id, o.amount FROM users AS u
JOIN Orders AS o ON o.user_id = u.user_id
WHERE o.order_date = '2026-09-30'
