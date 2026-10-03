SELECT COUNT(o.order_id) AS orders
FROM Orders AS o
JOIN order_items AS i ON i.order_id = o.order_id
WHERE o.order_date = '2026-09-30' AND i.sku = 'A-1'
