SELECT SUM(views) FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND (title = 'Python_(programming_language)' OR wiki = 'en')
