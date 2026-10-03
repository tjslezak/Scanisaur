SELECT term FROM web.trends WHERE refresh_date = '2026-09-30'
UNION DISTINCT
SELECT term FROM web.trends WHERE refresh_date = '2026-09-29'
