-- Reading all of a 13.5 MB table costs little: info, and the query may run.
SELECT sku, units FROM web.daily_sample
