SELECT SUM(views) FROM web.pageviews
WHERE EXTRACT(DATE FROM datehour) = '2025-06-01' AND wiki = 'en'
