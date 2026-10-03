SELECT AVG(o.amount) AS average
FROM Orders AS o
JOIN order_items AS i USING (order_id)
WHERE o.order_date = '2026-09-30'
