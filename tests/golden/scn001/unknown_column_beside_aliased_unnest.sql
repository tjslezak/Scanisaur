-- The GA4 shape: a typo in a base-table column next to an aliased UNNEST.
SELECT event_nme, p.value
FROM events, UNNEST(params) AS p
WHERE p.key = 'page'
