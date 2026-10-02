SELECT COUNT(*) FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND title BETWEEN 'A' AND 'B'
