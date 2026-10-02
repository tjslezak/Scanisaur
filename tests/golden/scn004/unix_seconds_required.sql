SELECT SUM(views) FROM web.pageviews
WHERE UNIX_SECONDS(datehour) >= 1748736000 AND wiki = 'en'
