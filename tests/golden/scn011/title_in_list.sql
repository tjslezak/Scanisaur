SELECT title, SUM(views) AS views FROM web.pageviews
WHERE DATE(datehour) = '2025-06-01' AND title IN ('Python_(programming_language)', 'Rust_(programming_language)')
GROUP BY title
