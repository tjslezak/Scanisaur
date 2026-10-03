-- 180,000 x 365 rows: under the warn threshold.
SELECT d.sku, c.day FROM web.daily_sample AS d CROSS JOIN calendar AS c
