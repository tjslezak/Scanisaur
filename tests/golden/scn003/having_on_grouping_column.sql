SELECT order_date, COUNT(*) AS orders FROM Orders
GROUP BY order_date
HAVING order_date = '2026-09-01'
