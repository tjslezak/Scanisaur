-- Both sizes are known from the catalog: 5 million x 400,000 rows.
SELECT COUNT(*) FROM users AS u CROSS JOIN web.store_sales AS s
