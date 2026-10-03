-- The ON clause filters one side only, so it relates nothing; the filter may leave
-- fewer rows, so the finding warns.
SELECT u.country, s.store
FROM users AS u JOIN web.store_sales AS s ON s.region = 'EU'
