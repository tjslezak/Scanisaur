SELECT wiki, SUM(views) AS views FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND wiki IN ('en', 'de') AND title = 'Python_(programming_language)'
GROUP BY wiki
