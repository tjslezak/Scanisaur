SELECT COUNT(*) FROM web.downloads
WHERE DATE(timestamp) = '2026-09-28' AND (LOWER(project) = 'requests' OR LOWER(project) = 'urllib3')
