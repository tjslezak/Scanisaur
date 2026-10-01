"""Facts extraction on a corpus of BigQuery queries (spike for issue #4)."""

import pytest
import sqlglot
from sqlglot.optimizer.qualify import qualify

from scanisaur.engine.facts import Predicate, QueryFacts, TableFacts, extract

SCHEMA: dict[str, object] = {
    "proj": {
        "analytics": {
            "events": {
                "event_date": "DATE",
                "event_ts": "TIMESTAMP",
                "user_id": "STRING",
                "event_name": "STRING",
                "params": "ARRAY<STRUCT<key STRING, value STRING>>",
                "device": "STRUCT<category STRING, os STRING>",
                "tags": "ARRAY<STRING>",
            },
            "users": {"user_id": "STRING", "country": "STRING", "signup_date": "DATE"},
            "events_archive": {"event_date": "DATE", "user_id": "STRING"},
            "raw_logs": {"payload": "STRING"},
            "plans": {"plan_id": "STRING", "valid_from": "DATE", "valid_to": "DATE"},
        },
        "ga4": {"events_*": {"event_name": "STRING", "user_pseudo_id": "STRING"}},
    }
}


def facts_for(sql: str) -> QueryFacts:
    tree = sqlglot.parse_one(sql, read="bigquery")
    qualified = qualify(tree, schema=SCHEMA, dialect="bigquery", expand_stars=False)
    return extract(qualified)


def table(facts: QueryFacts, alias: str) -> TableFacts:
    matches = [t for t in facts.tables if t.table.alias == alias]
    assert len(matches) == 1, f"expected one table aliased {alias!r}, got {facts.tables}"
    return matches[0]


def only_predicate(facts: TableFacts) -> Predicate:
    assert len(facts.predicates) == 1, facts.predicates
    return facts.predicates[0]


def test_literal_partition_filter() -> None:
    facts = facts_for(
        "SELECT user_id, event_name FROM `proj.analytics.events` WHERE event_date >= '2026-09-01'"
    )
    events = table(facts, "events")
    assert events.table.qualified_name == "proj.analytics.events"
    assert events.columns == {"user_id", "event_name", "event_date"}
    assert not events.star
    predicate = only_predicate(events)
    assert (predicate.column, predicate.op, predicate.constant) == ("event_date", ">=", True)
    assert predicate.values == ("'2026-09-01'",)
    assert predicate.wrapper is None


def test_select_star_without_filter() -> None:
    facts = facts_for("SELECT * FROM `proj.analytics.events`")
    events = table(facts, "events")
    assert events.star
    assert events.predicates == ()
    assert facts.outer_limit is None
    assert not facts.outer_aggregated


def test_function_wrapped_partition_column() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` WHERE DATE(event_ts) = '2026-09-01'"
    )
    predicate = only_predicate(table(facts, "events"))
    assert (predicate.column, predicate.wrapper, predicate.constant) == ("event_ts", "DATE", True)


def test_cast_wrapped_column() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` "
        "WHERE CAST(event_date AS STRING) = '2026-09-01'"
    )
    assert only_predicate(table(facts, "events")).wrapper == "CAST"


def test_inner_join_keys_and_per_table_filters() -> None:
    facts = facts_for(
        "SELECT e.user_id, u.country FROM `proj.analytics.events` e "
        "JOIN `proj.analytics.users` u ON e.user_id = u.user_id "
        "WHERE e.event_date = '2026-09-01' AND u.country = 'US'"
    )
    assert only_predicate(table(facts, "e")).column == "event_date"
    assert only_predicate(table(facts, "u")).column == "country"
    (join,) = facts.joins
    assert join.target == "u"
    assert join.target_kind == "table"
    assert join.has_condition
    assert join.keys == (("e.user_id", "u.user_id"),)


def test_comma_join_without_condition() -> None:
    facts = facts_for(
        "SELECT e.user_id, u.country FROM `proj.analytics.events` e, `proj.analytics.users` u"
    )
    (join,) = facts.joins
    assert join.target_kind == "table"
    assert not join.has_condition


def test_comma_join_with_condition_in_where() -> None:
    facts = facts_for(
        "SELECT e.user_id, u.country FROM `proj.analytics.events` e, `proj.analytics.users` u "
        "WHERE e.user_id = u.user_id"
    )
    (join,) = facts.joins
    assert join.has_condition
    assert join.keys == (("e.user_id", "u.user_id"),)


def test_using_becomes_join_keys() -> None:
    facts = facts_for(
        "SELECT * FROM `proj.analytics.events` e LEFT JOIN `proj.analytics.users` u USING (user_id)"
    )
    (join,) = facts.joins
    assert join.side == "LEFT"
    assert join.keys == (("e.user_id", "u.user_id"),)
    assert table(facts, "e").star
    assert table(facts, "u").star


def test_unnest_is_not_a_table_join() -> None:
    facts = facts_for(
        "SELECT e.user_id, p.key FROM `proj.analytics.events` e, UNNEST(e.params) AS p "
        "WHERE e.event_date = '2026-09-01'"
    )
    (join,) = facts.joins
    assert join.target_kind == "unnest"
    assert [t.table.alias for t in facts.tables] == ["e"]
    assert table(facts, "e").columns == {"user_id", "params", "event_date"}


def test_cte_with_relative_date_filter() -> None:
    facts = facts_for(
        "WITH recent AS (SELECT user_id FROM `proj.analytics.events` "
        "WHERE event_date > DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)) "
        "SELECT COUNT(DISTINCT user_id) AS users FROM recent"
    )
    predicate = only_predicate(table(facts, "events"))
    assert predicate.op == ">"
    assert predicate.constant
    assert facts.outer_aggregated


def test_cte_referenced_twice_scans_twice() -> None:
    facts = facts_for(
        "WITH x AS (SELECT user_id FROM `proj.analytics.events`) "
        "SELECT a.user_id FROM x a JOIN x b ON a.user_id = b.user_id"
    )
    assert table(facts, "events").scans == 2
    (join,) = facts.joins
    assert join.target_kind == "derived"


def test_unused_cte_is_not_scanned() -> None:
    facts = facts_for(
        "WITH unused AS (SELECT payload FROM `proj.analytics.raw_logs`) "
        "SELECT country FROM `proj.analytics.users`"
    )
    assert [t.table.name for t in facts.tables] == ["users"]


def test_subquery_in_from() -> None:
    facts = facts_for(
        "SELECT country, n FROM (SELECT country, COUNT(*) AS n FROM `proj.analytics.users` "
        "GROUP BY country) WHERE n > 10"
    )
    users = table(facts, "users")
    assert users.columns == {"country"}
    assert users.predicates == ()


def test_in_subquery_filter_is_not_constant() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` WHERE user_id IN "
        "(SELECT user_id FROM `proj.analytics.users` WHERE country = 'US')"
    )
    events_predicate = only_predicate(table(facts, "events"))
    assert (events_predicate.op, events_predicate.constant) == ("in", False)
    assert only_predicate(table(facts, "users")).values == ("'US'",)


def test_scalar_subquery_partition_filter_is_not_constant() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` WHERE event_date = "
        "(SELECT MAX(event_date) FROM `proj.analytics.events_archive`)"
    )
    predicate = only_predicate(table(facts, "events"))
    assert (predicate.column, predicate.constant) == ("event_date", False)


def test_wildcard_table_suffix_filter() -> None:
    facts = facts_for(
        "SELECT event_name FROM `proj.ga4.events_*` "
        "WHERE _TABLE_SUFFIX BETWEEN '20260901' AND '20260907'"
    )
    events = table(facts, "events_*")
    assert events.table.name == "events_*"
    predicate = only_predicate(events)
    assert (predicate.column, predicate.op) == ("_table_suffix", "between")
    assert predicate.values == ("'20260901'", "'20260907'")
    assert events.columns == {"event_name"}


def test_wildcard_table_without_suffix_filter() -> None:
    facts = facts_for("SELECT COUNT(*) AS n FROM `proj.ga4.events_*`")
    assert table(facts, "events_*").predicates == ()
    assert facts.outer_aggregated


def test_qualify_predicate_is_labeled() -> None:
    facts = facts_for(
        "SELECT user_id, event_ts FROM `proj.analytics.events` WHERE event_date = '2026-09-01' "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY event_ts DESC) = 1"
    )
    clauses = sorted(p.clause for p in table(facts, "events").predicates)
    assert clauses == ["qualify", "where"]
    assert not facts.outer_aggregated


def test_outer_limit() -> None:
    facts = facts_for("SELECT * FROM `proj.analytics.events` LIMIT 10")
    assert facts.outer_limit == 10


def test_union_all_branches() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` WHERE event_date = '2026-09-01' "
        "UNION ALL SELECT user_id FROM `proj.analytics.events_archive`"
    )
    assert sorted(t.table.name for t in facts.tables) == ["events", "events_archive"]
    assert table(facts, "events_archive").predicates == ()


def test_struct_field_reads_top_level_column() -> None:
    facts = facts_for("SELECT e.device.category FROM `proj.analytics.events` e")
    assert table(facts, "e").columns == {"device"}


def test_pipe_syntax() -> None:
    facts = facts_for(
        "FROM `proj.analytics.events` |> WHERE event_date = '2026-09-01' "
        "|> AGGREGATE COUNT(*) AS n GROUP BY event_name"
    )
    events = table(facts, "events")
    assert only_predicate(events).column == "event_date"
    assert facts.outer_aggregated


def test_star_except_records_excluded_columns() -> None:
    facts = facts_for("SELECT * EXCEPT (params, device) FROM `proj.analytics.events`")
    events = table(facts, "events")
    assert events.star
    assert events.star_except == {"params", "device"}


def test_or_of_equalities_is_treated_as_in() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` "
        "WHERE event_date = '2026-09-01' OR event_date = '2026-09-02'"
    )
    predicate = only_predicate(table(facts, "events"))
    assert (predicate.op, predicate.constant) == ("in", True)
    assert predicate.values == ("'2026-09-01'", "'2026-09-02'")


def test_or_across_columns_stays_other() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` "
        "WHERE event_date = '2026-09-01' OR user_id = 'abc'"
    )
    assert only_predicate(table(facts, "events")).op == "other"


def test_correlated_subquery_columns_count_for_outer_table() -> None:
    facts = facts_for(
        "SELECT e.event_name FROM `proj.analytics.events` e WHERE EXISTS "
        "(SELECT 1 FROM `proj.analytics.users` u WHERE u.user_id = e.user_id)"
    )
    assert table(facts, "e").columns == {"event_name", "user_id"}
    assert table(facts, "u").predicates == ()


def test_insert_select_reports_source_tables() -> None:
    tree = sqlglot.parse_one(
        "INSERT INTO `proj.analytics.users` (user_id) SELECT user_id FROM `proj.analytics.events`",
        read="bigquery",
    )
    facts = extract(qualify(tree, schema=SCHEMA, dialect="bigquery", expand_stars=False))
    assert [t.table.name for t in facts.tables] == ["events"]


def test_non_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a query"):
        extract(sqlglot.parse_one("DROP TABLE `proj.analytics.users`", read="bigquery"))


@pytest.mark.parametrize(
    ("sql", "op"),
    [
        ("WHERE '2026-09-01' <= event_date", ">="),
        ("WHERE event_date IN ('2026-09-01', '2026-09-02')", "in"),
        ("WHERE event_date IS NOT NULL", "other"),
    ],
)
def test_comparison_shapes(sql: str, op: str) -> None:
    facts = facts_for(f"SELECT user_id FROM `proj.analytics.events` {sql}")
    assert only_predicate(table(facts, "events")).op == op


# Regressions from the PR #5 review.


@pytest.mark.parametrize(
    ("condition", "column"),
    [
        ("CURRENT_DATE() BETWEEN valid_from AND valid_to", "valid_from"),
        ("'2026-09-01' IN (valid_from, valid_to)", "valid_from"),
        ("valid_from < valid_to", "valid_from"),
    ],
)
def test_conditions_without_a_single_column_side_are_other(condition: str, column: str) -> None:
    facts = facts_for(f"SELECT plan_id FROM `proj.analytics.plans` WHERE {condition}")
    predicate = only_predicate(table(facts, "plans"))
    assert (predicate.column, predicate.op, predicate.constant) == (column, "other", False)


def test_constant_in_unnest_of_column_is_other() -> None:
    facts = facts_for("SELECT e.user_id FROM `proj.analytics.events` e WHERE 'x' IN UNNEST(e.tags)")
    predicate = only_predicate(table(facts, "e"))
    assert (predicate.column, predicate.op) == ("tags", "other")


def test_in_unnest_of_constant_array_is_constant() -> None:
    facts = facts_for(
        "SELECT user_id FROM `proj.analytics.events` "
        "WHERE event_date IN UNNEST(['2026-09-01', '2026-09-02'])"
    )
    predicate = only_predicate(table(facts, "events"))
    assert (predicate.op, predicate.constant) == ("in", True)
    assert predicate.values == ("'2026-09-01'", "'2026-09-02'")


@pytest.mark.parametrize(
    ("condition", "column"),
    [
        ("(event_date) = '2026-09-01'", "event_date"),
        ("e.device.category = 'mobile'", "device"),
    ],
)
def test_parentheses_and_struct_fields_are_not_wrappers(condition: str, column: str) -> None:
    facts = facts_for(f"SELECT e.user_id FROM `proj.analytics.events` e WHERE {condition}")
    predicate = only_predicate(table(facts, "e"))
    assert (predicate.column, predicate.wrapper) == (column, None)


@pytest.mark.parametrize("condition", ["TRUE", "1 = 1", "u.country = 'US'"])
def test_on_clause_that_does_not_link_tables_is_no_condition(condition: str) -> None:
    facts = facts_for(
        "SELECT e.user_id FROM `proj.analytics.events` e "
        f"JOIN `proj.analytics.users` u ON {condition}"
    )
    (join,) = facts.joins
    assert not join.has_condition


def test_non_equi_join_condition_counts() -> None:
    facts = facts_for(
        "SELECT e.user_id FROM `proj.analytics.events` e JOIN `proj.analytics.plans` p "
        "ON e.event_date BETWEEN p.valid_from AND p.valid_to"
    )
    (join,) = facts.joins
    assert join.has_condition
    assert join.keys == ()


def test_aggregate_in_scalar_subquery_does_not_aggregate_outer_query() -> None:
    facts = facts_for(
        "SELECT (SELECT MAX(signup_date) FROM `proj.analytics.users`) AS latest, user_id "
        "FROM `proj.analytics.events`"
    )
    assert not facts.outer_aggregated


@pytest.mark.parametrize(
    ("second_branch", "aggregated"),
    [
        ("SELECT COUNT(*) AS n FROM `proj.analytics.events_archive`", True),
        ("SELECT 1 AS n FROM `proj.analytics.events_archive`", False),
    ],
)
def test_set_operation_is_aggregated_only_when_every_branch_is(
    second_branch: str, aggregated: bool
) -> None:
    facts = facts_for(
        f"SELECT COUNT(*) AS n FROM `proj.analytics.events` UNION ALL {second_branch}"
    )
    assert facts.outer_aggregated is aggregated
