SELECT SUM(views) FROM web.pageviews AS p
WHERE TIMESTAMP_TRUNC(p.datehour, DAY) = '2026-09-30'
