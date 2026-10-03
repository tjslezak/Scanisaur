-- The join key is missing: every user pairs with every order of the day. Orders isn't
-- listed by partition here, so its rows are an upper bound and the finding warns.
SELECT COUNT(*) FROM users AS u, Orders AS o
WHERE o.order_date = '2026-09-30'
