SELECT title, SUM(views) AS views FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01'
GROUP BY title
HAVING title = 'Python_(programming_language)'
