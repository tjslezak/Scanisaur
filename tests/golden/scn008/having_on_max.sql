SELECT e.user_id, MAX(e.event_ts) AS last_seen FROM events AS e
WHERE e.event_date >= '2026-09-01'
GROUP BY e.user_id
HAVING MAX(e.event_ts) <= '2026-09-15'
