-- policy: {"rules": {"SCN010": "block"}}
-- A severity override makes the warning a block.
SELECT user_id FROM events WHERE event_date IS NOT NULL
