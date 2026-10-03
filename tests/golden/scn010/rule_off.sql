-- policy: {"rules": {"SCN010": "off", "SCN003": "info"}}
SELECT user_id FROM events WHERE event_date IS NOT NULL
