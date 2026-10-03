-- A subquery inside IN UNNEST((...)) has its names checked like any other.
SELECT user_id FROM users
WHERE user_id IN UNNEST((SELECT ARRAY_AGG(user_id) FROM analytics.sessions))
