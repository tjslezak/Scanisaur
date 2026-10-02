-- An outer LIMIT 0 reads nothing, so no block is read either.
SELECT views FROM web.pageviews
WHERE DATE(datehour) = '2026-09-30' AND title = 'Python_(programming_language)'
LIMIT 0
