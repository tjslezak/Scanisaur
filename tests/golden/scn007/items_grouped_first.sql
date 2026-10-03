SELECT SUM(o.amount) AS revenue, SUM(i.items) AS items
FROM Orders AS o
JOIN (SELECT order_id, COUNT(*) AS items FROM order_items GROUP BY order_id) AS i
  ON i.order_id = o.order_id
WHERE o.order_date = '2026-09-30'
