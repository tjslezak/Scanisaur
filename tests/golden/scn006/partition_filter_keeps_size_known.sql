-- A partition filter evaluated against the listed partitions keeps the size known.
SELECT COUNT(*) FROM web.trends AS t CROSS JOIN web.daily_sample AS d
WHERE t.refresh_date = '2026-09-30'
