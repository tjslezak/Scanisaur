SELECT SUM(views) FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND title = 'Python_(programming_language)'
  AND wiki IN (SELECT region FROM web.store_sales)
