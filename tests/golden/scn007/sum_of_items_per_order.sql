SELECT o.order_id, SUM(i.price) AS total
FROM Orders AS o
JOIN order_items AS i ON i.order_id = o.order_id
WHERE o.order_date = '2026-09-30'
GROUP BY o.order_id
