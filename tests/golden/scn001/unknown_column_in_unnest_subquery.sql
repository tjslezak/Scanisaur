-- A subquery inside IN UNNEST((...)) has its columns checked like any other.
SELECT user_id FROM users
WHERE user_id IN UNNEST((SELECT ARRAY_AGG(usr_id) FROM events WHERE event_date = '2026-09-30'))
