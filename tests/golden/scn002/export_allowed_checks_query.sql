-- policy: {"read_only": false}
EXPORT DATA OPTIONS (uri = 'gs://bucket/users-*.csv', format = 'CSV') AS
SELECT user_id, contry FROM users
