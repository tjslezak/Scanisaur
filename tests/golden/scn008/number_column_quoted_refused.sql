SELECT title FROM web.pageviews AS p
WHERE p.datehour >= '2026-09-30' AND p.datehour < '2026-10-01' AND p.views > '1000'
