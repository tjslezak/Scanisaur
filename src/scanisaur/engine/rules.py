"""Rule identifiers. Each one has a page in the rule catalog."""

#: The SQL couldn't be analyzed (syntax error, script, several statements).
UNANALYZABLE = "SCN000"
#: A table, column or alias the catalog doesn't have, or an ambiguous column.
UNKNOWN_IDENTIFIER = "SCN001"
#: A write or DDL statement while the policy is read-only.
WRITE_STATEMENT = "SCN002"
#: No filter that lets BigQuery skip partitions or shards; blocks when the table requires one.
PARTITION_FILTER = "SCN003"
#: A function around a partition or cluster column that stops BigQuery from skipping data.
PRUNING_DEFEATED = "SCN004"
#: SELECT * that reads every column of a large table, LIMIT or not.
SELECT_STAR = "SCN005"
#: A filter on a later cluster column with none on the leading one, so clustering helps little.
CLUSTER_PREFIX = "SCN011"
