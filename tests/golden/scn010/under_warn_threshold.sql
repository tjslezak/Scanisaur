-- 900 MB at most: under the default warn threshold.
SELECT country, COUNT(*) AS n FROM users GROUP BY country
