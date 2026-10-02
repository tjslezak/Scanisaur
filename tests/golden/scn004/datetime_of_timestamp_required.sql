SELECT SUM(views) FROM web.pageviews
WHERE DATETIME(datehour) >= '2025-06-01' AND wiki = 'en'
