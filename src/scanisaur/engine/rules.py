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
#: Sources joined with nothing, or only a non-equality, relating them: every row pairs.
CROSS_JOIN = "SCN006"
#: A join on columns that aren't a unique key, which repeats rows a count or sum then counts.
FAN_OUT = "SCN007"
#: A comparison between types BigQuery refuses to compare, or compares in a way that
#: changes the answer: a date against a timestamp, an integer against a float.
TYPE_MISMATCH = "SCN008"
#: A query that returns every row of a large table: no LIMIT, aggregate or filter bounds it.
UNBOUNDED_RESULT = "SCN009"
#: The estimated bytes billed reach the policy's warn or block threshold.
SCAN_THRESHOLD = "SCN010"
#: A filter on a later cluster column with none on the leading one, so clustering helps little.
CLUSTER_PREFIX = "SCN011"

#: Every rule, in order. A policy can override the severity of any of them.
ALL_RULES = (
    UNANALYZABLE,
    UNKNOWN_IDENTIFIER,
    WRITE_STATEMENT,
    PARTITION_FILTER,
    PRUNING_DEFEATED,
    SELECT_STAR,
    CROSS_JOIN,
    FAN_OUT,
    TYPE_MISMATCH,
    UNBOUNDED_RESULT,
    SCAN_THRESHOLD,
    CLUSTER_PREFIX,
)
