SELECT COUNT(*) FROM shop.visits AS v
WHERE v.started_at > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
AND v.started_at <= CURRENT_DATE()
