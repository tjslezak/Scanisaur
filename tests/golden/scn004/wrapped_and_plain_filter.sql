SELECT term FROM web.trends
WHERE CAST(refresh_date AS STRING) LIKE '2026-09%' AND refresh_date >= '2026-09-01'
