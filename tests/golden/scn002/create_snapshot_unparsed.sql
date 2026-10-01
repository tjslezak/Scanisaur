-- sqlglot keeps this as a raw command; its CREATE keyword still makes it a write.
CREATE SNAPSHOT TABLE analytics.events_snapshot CLONE analytics.events
