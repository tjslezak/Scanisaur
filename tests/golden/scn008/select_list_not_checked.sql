SELECT title, CASE WHEN p.datehour = '2026-09-30' THEN 'midnight' END AS label
FROM web.pageviews AS p
WHERE p.datehour >= '2026-09-30' AND p.datehour < '2026-10-01'
