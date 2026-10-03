-- An OR of equalities is still matched by value (measured in #23).
SELECT COUNT(*) FROM Orders AS o
JOIN users AS u ON o.user_id = u.user_id OR o.order_id = u.user_id
WHERE o.order_date = '2026-09-30'
