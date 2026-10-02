SELECT SUM(views) FROM web.pageviews
WHERE TIMESTAMP_TRUNC(datehour, DAY) = TIMESTAMP '2025-06-01' AND wiki = 'en'
