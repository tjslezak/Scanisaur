WITH sold AS (SELECT DISTINCT order_id FROM order_items WHERE sku = 'A-1')
SELECT SUM(o.amount) AS revenue
FROM Orders AS o
JOIN sold ON sold.order_id = o.order_id
WHERE o.order_date = '2026-09-30'
