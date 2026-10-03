SELECT o.order_id, i.sku
FROM Orders AS o
JOIN order_items AS i ON i.user_id = o.user_id
WHERE o.order_date = '2026-09-30'
