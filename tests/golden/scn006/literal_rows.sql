-- Two literal rows pair with each user once each.
SELECT u.user_id, k.kind
FROM users AS u CROSS JOIN (SELECT 'a' AS kind UNION ALL SELECT 'b') AS k
