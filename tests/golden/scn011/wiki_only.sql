SELECT title, SUM(views) AS views FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND wiki = 'en'
GROUP BY title
