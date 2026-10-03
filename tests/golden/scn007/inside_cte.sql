WITH revenue AS (
  SELECT o.user_id, SUM(o.amount) AS amount
  FROM Orders AS o
  JOIN order_items AS i ON i.order_id = o.order_id
  WHERE o.order_date = '2026-09-30'
  GROUP BY o.user_id
)
SELECT u.country, SUM(r.amount) AS amount
FROM users AS u
JOIN revenue AS r ON r.user_id = u.user_id
GROUP BY u.country
