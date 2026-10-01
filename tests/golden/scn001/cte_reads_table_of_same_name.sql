WITH orders AS (SELECT * FROM Orders WHERE order_date = '2026-09-01')
SELECT order_id, amount FROM orders
