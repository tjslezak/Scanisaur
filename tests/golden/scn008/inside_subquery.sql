SELECT u.country FROM users AS u
WHERE u.user_id IN (
  SELECT e.user_id FROM events AS e
  WHERE e.event_date = '2026-09-30' AND e.event_ts <= '2026-09-30'
)
