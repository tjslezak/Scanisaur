SELECT country_code, COUNT(*) AS downloads FROM web.downloads
WHERE DATE(timestamp) = '2026-09-30' AND project = 'requests'
GROUP BY country_code
