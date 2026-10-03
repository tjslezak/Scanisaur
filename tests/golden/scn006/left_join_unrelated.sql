SELECT u.user_id, s.store
FROM users AS u LEFT JOIN web.store_sales AS s ON s.region = 'EU'
