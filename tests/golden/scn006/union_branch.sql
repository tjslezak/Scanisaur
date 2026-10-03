SELECT u.user_id FROM users AS u JOIN Orders AS o ON o.user_id = u.user_id
WHERE o.order_date = '2026-09-30'
UNION ALL
SELECT u.user_id FROM users AS u, web.store_sales AS s
