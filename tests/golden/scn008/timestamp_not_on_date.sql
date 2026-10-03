SELECT COUNT(*) FROM web.pageviews AS p
WHERE p.datehour >= '2026-09-29' AND p.datehour < '2026-10-01' AND p.datehour != '2026-09-30'
