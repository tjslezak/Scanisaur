SELECT u.user_id, DATE_DIFF(t.today, u.signup_date, DAY) AS age
FROM users AS u CROSS JOIN (SELECT CURRENT_DATE() AS today) AS t
