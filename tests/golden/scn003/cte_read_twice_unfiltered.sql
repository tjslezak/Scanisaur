WITH e AS (SELECT user_id FROM events)
SELECT a.user_id FROM e AS a JOIN e AS b USING (user_id)
