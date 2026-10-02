SELECT title, views FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01'
QUALIFY title = 'Python_(programming_language)'
