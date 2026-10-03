-- policy: {"warn_bytes": 10000000}
-- At low confidence the low end, 10.5 MB, is read, and it reaches a 10 MB threshold.
SELECT user_id FROM events WHERE event_date = '2026-09-01'
