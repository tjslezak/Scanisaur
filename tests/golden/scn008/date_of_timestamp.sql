SELECT SUM(views) FROM web.pageviews AS p
WHERE DATE(p.datehour) = '2026-09-30'
