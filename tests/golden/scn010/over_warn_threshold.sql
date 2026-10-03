-- Every partition of events, read for one column: about 347 GB, over 100 GiB.
SELECT user_id FROM events WHERE event_date IS NOT NULL
