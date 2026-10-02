SELECT e.user_id, o.amount
FROM events AS e
JOIN Orders AS o ON o.user_id = e.user_id AND o.order_date = '2026-09-01'
WHERE e.event_date = '2026-09-01'
