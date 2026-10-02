-- A function of the whole array reads every field of its elements (#22).
SELECT ARRAY_LENGTH(params) AS n FROM events
WHERE event_date = '2026-09-30'
