SELECT c.country, COUNT(*) AS visits
FROM shop.visits AS v
JOIN shop.visitors AS c ON v.visitor_id = c.visitor_id
GROUP BY c.country
