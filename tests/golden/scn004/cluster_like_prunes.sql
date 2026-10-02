SELECT COUNT(*) FROM web.downloads
WHERE DATE(timestamp) = '2026-09-28' AND project LIKE 'requests%'
