WITH day AS (
  SELECT term, rank FROM web.trends WHERE refresh_date = '2026-09-30'
)
SELECT * FROM day ORDER BY rank
