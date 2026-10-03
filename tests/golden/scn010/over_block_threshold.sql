-- policy: {"block_bytes": 300000000000}
-- user_id is 346.8 GB across every partition of events: sure to pass the block threshold.
SELECT user_id FROM events WHERE event_date IS NOT NULL
