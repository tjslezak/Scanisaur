-- A table read inside IN UNNEST((...)) still needs its required partition filter.
SELECT user_id FROM users
WHERE user_id IN UNNEST((SELECT ARRAY_AGG(user_id) FROM Orders))
