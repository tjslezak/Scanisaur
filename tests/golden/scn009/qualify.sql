SELECT user_id, country FROM users
QUALIFY ROW_NUMBER() OVER (PARTITION BY country ORDER BY signup_date) = 1
