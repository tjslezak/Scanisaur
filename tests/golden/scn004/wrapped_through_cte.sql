WITH t AS (SELECT FORMAT_DATE('%Y-%m', refresh_date) AS month, term FROM web.trends)
SELECT term FROM t WHERE month = '2026-09'
