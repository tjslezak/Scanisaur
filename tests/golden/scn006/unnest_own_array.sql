SELECT p.key, COUNT(*) AS n FROM events AS e, UNNEST(e.params) AS p
WHERE e.event_date = '2026-09-30'
GROUP BY p.key
