LOAD DATA INTO analytics.events
FROM FILES (format = 'CSV', uris = ['gs://bucket/events.csv'])
