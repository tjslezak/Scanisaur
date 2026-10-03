-- WITH OFFSET names the position; the alias names each element.
SELECT e.user_id, p.key, p.value, pos
FROM events e, UNNEST(e.params) AS p WITH OFFSET AS pos
WHERE e.event_date = '2026-09-01' AND pos < 3
