-- An array of scalars exposes only its alias.
SELECT n, nope
FROM UNNEST([1, 2, 3]) AS n
