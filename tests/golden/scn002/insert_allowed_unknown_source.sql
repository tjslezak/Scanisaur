-- policy: {"read_only": false}
INSERT INTO users (user_id)
SELECT user_id FROM event_log
