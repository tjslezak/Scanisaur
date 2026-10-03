WITH pairs AS (SELECT u.user_id, s.store FROM users AS u, web.store_sales AS s)
SELECT COUNT(*) FROM pairs
