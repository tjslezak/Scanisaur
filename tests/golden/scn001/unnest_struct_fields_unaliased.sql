-- Fields of an unaliased UNNEST of STRUCTs are bare columns in BigQuery; this must pass.
SELECT event_date, key, value
FROM events, UNNEST(params)
WHERE event_date = '2026-09-01'
