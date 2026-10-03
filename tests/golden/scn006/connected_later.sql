-- The first join has no condition of its own, but WHERE relates all three; BigQuery
-- reorders the joins, so nothing pairs (measured in #23).
SELECT COUNT(*) FROM users AS u, Orders AS o, events AS e
WHERE e.user_id = u.user_id AND o.user_id = e.user_id
  AND o.order_date = '2026-09-30' AND e.event_date = '2026-09-30'
