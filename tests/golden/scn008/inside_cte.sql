WITH recent AS (
  SELECT title, views FROM web.pageviews AS p
  WHERE p.datehour BETWEEN '2026-09-29' AND '2026-09-30'
)
SELECT title, SUM(views) AS views FROM recent GROUP BY title
