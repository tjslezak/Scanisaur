-- An unlisted partition: 10.5 MB to 356 GB at low confidence. The high end assumes no
-- partition is skipped, so it is not held against the query.
SELECT user_id FROM events WHERE event_date = '2026-09-01'
