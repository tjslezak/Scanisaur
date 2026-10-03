-- One PyPI day: 98.5 GB with LOWER(project), 0.84 GB with project = 'requests'.
SELECT COUNT(*) FROM web.downloads
WHERE DATE(timestamp) = '2026-09-28' AND LOWER(project) = 'requests'
