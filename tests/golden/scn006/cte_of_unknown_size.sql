-- A CTE may hold any number of rows, so the finding warns.
WITH recent AS (SELECT user_id FROM events WHERE event_date = '2026-09-30')
SELECT COUNT(*) FROM recent AS r, web.store_sales AS s
