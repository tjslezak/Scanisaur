-- Reads only the NULL partition.
SELECT order_id FROM Orders WHERE order_date IS NULL
