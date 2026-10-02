SELECT name, licenses FROM web.packages
WHERE DATE(snapshot_at) = '2026-09-28' AND version = '1.0.0'
