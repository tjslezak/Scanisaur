-- events and users are keyed; store_sales is joined to neither.
SELECT COUNT(*) FROM events AS e
JOIN users AS u ON u.user_id = e.user_id
CROSS JOIN web.store_sales AS s
WHERE e.event_date = '2026-09-30'
