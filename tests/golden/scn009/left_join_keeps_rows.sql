SELECT u.user_id, o.amount FROM users AS u
LEFT JOIN Orders AS o ON o.user_id = u.user_id AND o.order_date = '2026-09-30'
