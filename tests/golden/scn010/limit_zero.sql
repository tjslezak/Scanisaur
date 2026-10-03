-- policy: {"warn_bytes": 1}
-- LIMIT 0 reads nothing, so even a one-byte threshold is not reached.
SELECT user_id FROM events LIMIT 0
