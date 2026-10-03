WITH top_users AS (SELECT user_id FROM users LIMIT 10)
SELECT t.user_id, s.store FROM top_users AS t CROSS JOIN web.store_sales AS s
