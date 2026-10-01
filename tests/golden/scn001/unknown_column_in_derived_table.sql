SELECT t.country, t.signup
FROM (SELECT user_id, country FROM users) AS t
