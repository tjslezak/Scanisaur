MERGE users t
USING (SELECT DISTINCT user_id FROM events) s
ON t.user_id = s.user_id
WHEN NOT MATCHED THEN INSERT (user_id) VALUES (s.user_id)
