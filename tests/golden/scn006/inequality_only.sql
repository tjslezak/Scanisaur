-- No equality: BigQuery compares every pair (measured in #23).
SELECT COUNT(*) FROM users AS u
JOIN web.trends AS t ON t.refresh_date > u.signup_date
