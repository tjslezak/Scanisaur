FROM events
|> WHERE evnt_date = '2026-09-01'
|> AGGREGATE COUNT(*) AS n GROUP BY event_name
