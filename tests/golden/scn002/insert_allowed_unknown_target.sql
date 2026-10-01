-- policy: {"read_only": false}
INSERT INTO user_archive (user_id)
SELECT user_id FROM users
