SELECT COUNT(u.user_id) AS buyers
FROM users AS u
JOIN Orders AS o ON o.user_id = u.user_id
JOIN order_items AS i ON i.order_id = o.order_id
WHERE o.order_date = '2026-09-30'
