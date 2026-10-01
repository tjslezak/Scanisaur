"""Rule identifiers. Each one has a page in the rule catalog."""

#: The SQL couldn't be analyzed (syntax error, script, several statements).
UNANALYZABLE = "SCN000"
#: A table, column or alias the catalog doesn't have, or an ambiguous column.
UNKNOWN_IDENTIFIER = "SCN001"
#: A write or DDL statement while the policy is read-only.
WRITE_STATEMENT = "SCN002"
