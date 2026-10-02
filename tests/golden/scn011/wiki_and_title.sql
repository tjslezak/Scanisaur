SELECT SUM(views) FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND wiki = 'en' AND title = 'Python_(programming_language)'
