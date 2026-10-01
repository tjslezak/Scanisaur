WITH base AS (SELECT user_id, event_name FROM events)
SELECT * EXCEPT (evnt_name) FROM base
