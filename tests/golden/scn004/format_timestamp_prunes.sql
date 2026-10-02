SELECT SUM(views) FROM web.pageviews
WHERE FORMAT_TIMESTAMP('%Y-%m-%d', datehour) = '2025-06-01' AND wiki = 'en'
