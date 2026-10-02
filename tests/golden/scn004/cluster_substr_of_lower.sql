SELECT COUNT(*) FROM web.downloads
WHERE DATE(timestamp) = '2026-09-28' AND SUBSTR(LOWER(project), 1, 3) = 'req'
