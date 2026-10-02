WITH wikis AS (SELECT wiki FROM UNNEST(['en', 'de']) AS wiki)
SELECT SUM(p.views)
FROM web.pageviews AS p
WHERE DATE(p.datehour) = '2025-06-01' AND p.title = 'Python_(programming_language)'
  AND EXISTS (SELECT 1 FROM wikis AS w WHERE w.wiki = p.wiki)
