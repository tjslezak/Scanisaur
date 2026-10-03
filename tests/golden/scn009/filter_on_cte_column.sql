WITH t AS (SELECT term, rank, refresh_date FROM web.trends)
SELECT term, rank FROM t WHERE refresh_date = '2026-09-30'
