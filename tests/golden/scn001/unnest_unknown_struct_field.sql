SELECT e.user_id, p.vaule
FROM events e, UNNEST(e.params) AS p
