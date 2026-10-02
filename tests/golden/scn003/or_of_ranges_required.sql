-- Year-over-year windows: BigQuery prunes an OR of ranges on the partition column.
SELECT order_id FROM Orders
WHERE order_date BETWEEN '2025-09-01' AND '2025-09-07'
   OR order_date BETWEEN '2026-09-01' AND '2026-09-07'
