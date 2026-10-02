WITH day AS (
  SELECT wiki, title, views FROM web.pageviews WHERE DATE(datehour) = '2025-06-01'
)
SELECT SUM(views) FROM day WHERE title = 'Python_(programming_language)'
