SELECT s.region, SUM(p.views) AS views
FROM web.store_sales AS s
JOIN web.pageviews AS p ON p.title = 'Python_(programming_language)' AND DATE(datehour) = '2025-06-01'
GROUP BY s.region
