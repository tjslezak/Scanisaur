SELECT term FROM web.trends WHERE EXTRACT(WEEK(MONDAY) FROM refresh_date) = 3
