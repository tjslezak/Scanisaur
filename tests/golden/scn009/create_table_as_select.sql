-- policy: {"read_only": false}
CREATE TABLE analytics.users_copy AS SELECT user_id, country FROM users
