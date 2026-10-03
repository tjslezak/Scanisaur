SELECT COUNT(*) FROM web.downloads
WHERE DATE(timestamp) = '2026-09-28' AND SUBSTR(project, 1, 8) = 'requests'
