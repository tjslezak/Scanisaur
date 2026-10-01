SELECT p.key
FROM events e, UNNEST(e.paramz) AS p
