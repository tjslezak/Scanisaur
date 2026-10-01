SELECT *
FROM ML.PREDICT(MODEL analytics.churn, (SELECT user_id, country FROM users))
