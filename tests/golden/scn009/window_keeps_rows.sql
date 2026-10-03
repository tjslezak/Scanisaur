SELECT user_id, ROW_NUMBER() OVER (ORDER BY signup_date) AS n FROM users
