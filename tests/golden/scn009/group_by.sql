SELECT term, MIN(rank) AS best FROM web.trends
WHERE refresh_date = '2026-09-30'
GROUP BY term
