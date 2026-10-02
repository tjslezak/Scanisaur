SELECT term FROM web.trends
WHERE CAST(EXTRACT(DAY FROM refresh_date) AS STRING) = '30'
