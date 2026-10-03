-- Every user with every day of the year: 1.8 billion pairs, between the thresholds.
SELECT u.user_id, c.day FROM users AS u CROSS JOIN calendar AS c
