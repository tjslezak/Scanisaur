WITH recent AS (SELECT user_id, amount FROM Orders WHERE order_date >= '2026-09-01')
SELECT u.country, COUNT(u.user_id) AS buyers
FROM users AS u
JOIN recent AS r ON r.user_id = u.user_id
GROUP BY u.country
