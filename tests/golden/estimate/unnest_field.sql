-- A field reached through UNNEST is billed on its own, like a struct field (#22):
-- this reads `value`, one of the two fields of `params`.
SELECT COUNT(DISTINCT p.value) AS values_seen FROM events, UNNEST(params) AS p
WHERE event_date = '2026-09-30'
