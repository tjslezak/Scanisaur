-- An outer LIMIT 0 reads nothing.
SELECT * FROM users AS u CROSS JOIN web.store_sales AS s LIMIT 0
