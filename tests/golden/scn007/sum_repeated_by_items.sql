SELECT SUM(o.amount) AS revenue
FROM Orders AS o
JOIN order_items AS i ON i.order_id = o.order_id
WHERE o.order_date = '2026-09-30'
