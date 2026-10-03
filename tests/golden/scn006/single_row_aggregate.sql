SELECT u.user_id, u.signup_date = m.latest AS newest
FROM users AS u CROSS JOIN (SELECT MAX(signup_date) AS latest FROM users) AS m
