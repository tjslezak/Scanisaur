SELECT user_id, (SELECT COUNT(*) FROM calendar) AS days FROM users
