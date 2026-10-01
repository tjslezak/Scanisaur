-- Known gap: next to an UNNEST whose columns aren't modeled, unqualified names
-- aren't checked, to avoid blocking valid struct-field references.
SELECT nope
FROM events, UNNEST(params)
