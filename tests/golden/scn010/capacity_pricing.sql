-- policy: {"price_per_tib": null}
-- Capacity pricing has no dollars, but bytes still count.
SELECT user_id FROM events WHERE event_date IS NOT NULL
