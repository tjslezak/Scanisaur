CREATE TABLE analytics.daily_users AS
SELECT event_date, COUNT(DISTINCT user_id) AS users FROM events GROUP BY event_date
