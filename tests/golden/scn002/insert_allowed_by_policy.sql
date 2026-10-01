-- policy: {"read_only": false}
INSERT INTO users (user_id, country)
SELECT user_id, 'US' FROM events
