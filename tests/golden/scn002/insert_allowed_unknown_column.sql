-- policy: {"read_only": false}
INSERT INTO users (user_id, contry)
SELECT user_id, 'US' FROM events
