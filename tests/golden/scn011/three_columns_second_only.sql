SELECT version, licenses FROM web.packages
WHERE DATE(snapshot_at) = '2026-09-28' AND name = 'requests'
