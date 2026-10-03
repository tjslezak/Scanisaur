WITH recent AS (SELECT order_id, user_id FROM Orders WHERE order_date >= '2026-09-01')
SELECT SUM(i.price) AS spent
FROM order_items AS i
JOIN recent AS r ON r.order_id = i.order_id
