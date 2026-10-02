-- BigQuery still rejects this, because the table requires a partition filter.
SELECT COUNT(*) FROM Orders
