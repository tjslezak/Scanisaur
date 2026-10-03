SELECT e.event_name, u.country FROM events AS e
JOIN users AS u ON u.user_id = e.user_id
WHERE e.event_date = '2026-09-30'
