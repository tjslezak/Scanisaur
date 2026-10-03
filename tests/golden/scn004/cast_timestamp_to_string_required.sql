SELECT SUM(views) FROM web.pageviews
WHERE CAST(datehour AS STRING) >= '2025-06-01' AND wiki = 'en'
