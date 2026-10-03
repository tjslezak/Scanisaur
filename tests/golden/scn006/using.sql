SELECT o.amount, u.country FROM Orders AS o JOIN users AS u USING (user_id)
WHERE o.order_date = '2026-09-30'
