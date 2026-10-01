SELECT *
FROM VECTOR_SEARCH(TABLE analytics.events, 'params', (SELECT params FROM events LIMIT 1), top_k => 5)
