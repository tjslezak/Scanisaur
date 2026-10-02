WITH wikis AS (SELECT wiki FROM UNNEST(['en', 'de']) AS wiki)
SELECT SUM(p.views)
FROM web.pageviews AS p
JOIN wikis AS w ON p.wiki = w.wiki
WHERE DATE(p.datehour) = '2025-06-01' AND p.title = 'Python_(programming_language)'
