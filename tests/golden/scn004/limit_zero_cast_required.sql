-- An outer LIMIT 0 reads nothing, and BigQuery accepts it even on a table that requires
-- a partition filter.
SELECT title FROM web.pageviews WHERE CAST(datehour AS STRING) LIKE '2026-09-30%' LIMIT 0
