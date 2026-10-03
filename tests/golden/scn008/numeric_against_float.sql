SELECT COUNT(*) FROM Orders AS o
JOIN web.scores AS s ON o.amount = s.score
WHERE o.order_date = '2026-09-30' AND s.bucket = 3
