SELECT COUNT(*) FROM events AS e
WHERE e.event_date = '2026-09-30' AND e.user_id = @user
