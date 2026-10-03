-- A date spine from GENERATE_DATE_ARRAY is a short list, not a table.
SELECT day, COUNT(*) AS signed_up
FROM users AS u CROSS JOIN UNNEST(GENERATE_DATE_ARRAY('2026-09-01', '2026-09-30')) AS day
WHERE u.signup_date <= day
GROUP BY day
