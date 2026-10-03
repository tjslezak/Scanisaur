SELECT u.country, s.score FROM users AS u
JOIN web.scores AS s ON u.user_id = s.bucket
WHERE s.bucket BETWEEN 0 AND 9
