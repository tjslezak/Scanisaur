-- Orders requires a partition filter, so BigQuery rejects this and bills nothing.
SELECT amount FROM Orders
