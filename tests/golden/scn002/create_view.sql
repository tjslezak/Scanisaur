CREATE OR REPLACE VIEW analytics.recent_events AS
SELECT * FROM events WHERE event_date >= '2026-09-01'
