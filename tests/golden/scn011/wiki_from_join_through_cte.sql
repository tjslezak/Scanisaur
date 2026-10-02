WITH wikis AS (SELECT wiki FROM UNNEST(['en', 'de']) AS wiki),
day AS (
  SELECT wiki, title, views FROM web.pageviews WHERE DATE(datehour) = '2025-06-01'
)
SELECT SUM(d.views)
FROM day AS d
JOIN wikis AS w ON d.wiki = w.wiki
WHERE d.title = 'Python_(programming_language)'
