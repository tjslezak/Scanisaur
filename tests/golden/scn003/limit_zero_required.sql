-- An outer LIMIT 0 reads nothing, and BigQuery accepts it even without the required filter.
SELECT * FROM Orders LIMIT 0
