SELECT e.user_id, p.key, p.value
FROM events e, UNNEST(e.params) AS p
WHERE e.event_date = '2026-09-01'
