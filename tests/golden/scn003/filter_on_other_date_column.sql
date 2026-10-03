-- The Trends trap: week is a date, but refresh_date is the partition column.
SELECT DISTINCT term, rank FROM web.trends WHERE week = '2026-09-20' AND rank <= 5
