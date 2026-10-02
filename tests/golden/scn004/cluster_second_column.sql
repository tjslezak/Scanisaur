SELECT SUM(views) FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND wiki = 'en' AND LOWER(title) = 'python_(programming_language)'
