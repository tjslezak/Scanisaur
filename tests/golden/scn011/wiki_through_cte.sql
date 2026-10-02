WITH day AS (
  SELECT wiki, title, views FROM web.pageviews WHERE DATE(datehour) = '2025-06-01' AND title = 'Python_(programming_language)'
)
SELECT SUM(views) FROM day WHERE wiki = 'en'
