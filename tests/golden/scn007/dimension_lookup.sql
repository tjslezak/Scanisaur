SELECT u.country, SUM(o.amount) AS revenue
FROM Orders AS o
JOIN users AS u ON u.user_id = o.user_id
WHERE o.order_date = '2026-09-30'
GROUP BY u.country
