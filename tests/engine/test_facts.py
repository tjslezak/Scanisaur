"""Facts extraction on a corpus of BigQuery queries (spike #4, made robust in #6)."""

import pytest

from scanisaur.catalog import Catalog, Column, Partitioning, Table
from scanisaur.engine import facts as facts_module
from scanisaur.engine.facts import (
    DerivedSource,
    FactsError,
    Predicate,
    QueryFacts,
    TableFacts,
    facts_from_sql,
)


def columns(**types: str) -> tuple[Column, ...]:
    return tuple(Column(name, type_) for name, type_ in types.items())


CATALOG = Catalog(
    tables=(
        Table(
            "proj",
            "analytics",
            "events",
            columns(
                event_date="DATE",
                event_ts="TIMESTAMP",
                user_id="STRING",
                event_name="STRING",
                params="ARRAY<STRUCT<key STRING, value STRING>>",
                device="STRUCT<category STRING, os STRING>",
                tags="ARRAY<STRING>",
            ),
            partitioning=Partitioning("event_date", "DAY"),
            clustering=("user_id",),
        ),
        Table(
            "proj",
            "analytics",
            "users",
            columns(user_id="STRING", country="STRING", signup_date="DATE"),
        ),
        Table("proj", "analytics", "events_archive", columns(event_date="DATE", user_id="STRING")),
        Table("proj", "analytics", "raw_logs", columns(payload="STRING")),
        Table(
            "proj",
            "analytics",
            "plans",
            columns(plan_id="STRING", valid_from="DATE", valid_to="DATE"),
        ),
        Table("proj", "ga4", "events_*", columns(event_name="STRING", user_pseudo_id="STRING")),
    ),
    default_project="proj",
    default_dataset="analytics",
)


def facts_for(sql: str) -> QueryFacts:
    return facts_from_sql(sql, CATALOG)


def table(facts: QueryFacts, alias: str) -> TableFacts:
    matches = [t for t in facts.tables if t.alias == alias]
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


def test_conditions_across_sources_link_columns() -> None:
    facts = facts_for(
        "SELECT e.user_id FROM `proj.analytics.events` e "
        "JOIN `proj.analytics.users` u ON e.user_id = u.user_id "
        "WHERE e.event_date = '2026-09-01' AND u.signup_date <= e.event_date"
    )
    assert table(facts, "e").linked == {"user_id", "event_date"}
    assert table(facts, "u").linked == {"user_id", "signup_date"}


def test_outer_join_links_both_sides() -> None:
    # A WHERE filter on u could make it an inner join; linking both sides stays safe.
    facts = facts_for(
        "SELECT e.user_id FROM `proj.analytics.events` e "
        "LEFT JOIN `proj.analytics.users` u ON e.user_id = u.user_id"
    )
    assert table(facts, "e").linked == {"user_id"}
    assert table(facts, "u").linked == {"user_id"}


def test_links_reach_tables_under_ctes_and_unions() -> None:
    facts = facts_for(
        "WITH d AS (SELECT user_id, event_name FROM `proj.analytics.events` "
        "UNION ALL SELECT user_id, 'x' FROM `proj.analytics.events_archive`) "
        "SELECT d.event_name FROM d JOIN `proj.analytics.users` u ON d.user_id = u.user_id"
    )
    assert table(facts, "events").linked == {"user_id"}
    assert table(facts, "events_archive").linked == {"user_id"}
    assert table(facts, "events").predicates == ()  # links aren't filters


def test_correlated_subquery_links_the_outer_column() -> None:
    facts = facts_for(
        "SELECT e.event_name FROM `proj.analytics.events` e WHERE EXISTS "
        "(SELECT 1 FROM `proj.analytics.users` u WHERE u.user_id = e.user_id)"
    )
    assert table(facts, "e").linked == {"user_id"}
    assert table(facts, "u").linked == {"user_id"}  # the correlation limits both sides


def test_subquery_with_its_own_source_of_that_name_is_not_correlated() -> None:
    facts = facts_for(
        "SELECT e.event_name FROM `proj.analytics.events` e WHERE e.user_id IN "
        "(SELECT e.user_id FROM `proj.analytics.users` e WHERE e.country = 'US')"
    )
    events = next(t for t in facts.tables if t.table.name == "events")
    assert events.linked == frozenset()


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
    assert [t.alias for t in facts.tables] == ["e"]
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
    assert table(facts, "e").paths == frozenset({("device", "category")})


@pytest.mark.parametrize(
    ("sql", "paths"),
    [
        ("SELECT user_id FROM events", None),  # whole columns only
        ("SELECT device.category, user_id FROM events", {("device", "category"), ("user_id",)}),
        ("SELECT device FROM events WHERE device.os = 'x'", {("device",), ("device", "os")}),
        ("SELECT device.* FROM events", {("device", "category"), ("device", "os")}),
        ("SELECT p.key FROM events, UNNEST(params) AS p", {("params", "key")}),
        (
            "SELECT p.value FROM events AS e CROSS JOIN UNNEST(e.params) AS p WITH OFFSET AS i "
            "WHERE p.key = 'a' AND i = 0",
            {("params", "key"), ("params", "value")},
        ),
        (
            "SELECT (SELECT value FROM UNNEST(params) WHERE key = 'a') FROM events",
            {("params", "key"), ("params", "value")},
        ),
        ("SELECT t FROM events, UNNEST(tags) AS t", None),  # an array of scalars
        ("SELECT ARRAY_LENGTH(params) FROM events", None),
        ("SELECT COUNT(*) FROM events, UNNEST(params)", None),
        ("SELECT TO_JSON_STRING(p) FROM events, UNNEST(params) AS p", None),
        ("WITH b AS (SELECT * FROM events) SELECT device.os FROM b", {("device", "os")}),
        (
            "WITH b AS (SELECT device AS d FROM events) SELECT x.d.category FROM b AS x",
            {("device", "category")},
        ),
        ("WITH b AS (SELECT DISTINCT device FROM events) SELECT device.os FROM b", None),
        (
            # Grouping reads the whole struct, which covers the field.
            "WITH b AS (SELECT device AS d FROM events GROUP BY d) SELECT d.os FROM b",
            {("device",), ("device", "os")},
        ),
        (
            "WITH b AS (SELECT device FROM events UNION ALL SELECT device FROM events) "
            "SELECT device.os FROM b",
            {("device", "os")},
        ),
        (
            "WITH b AS (SELECT device FROM events) "
            "SELECT device.os FROM b UNION ALL SELECT TO_JSON_STRING(device) FROM b",
            None,  # one reader reads the whole column, so the visit both share does
        ),
        (
            # GROUP BY ALL groups by every field of the struct.
            "WITH b AS (SELECT device AS d, COUNT(*) AS n FROM events GROUP BY ALL) "
            "SELECT d.os FROM b",
            None,
        ),
        (
            # `v` isn't read, so BigQuery drops it and `params.value` with it.
            "WITH x AS (SELECT p.key AS k, p.value AS v FROM events, UNNEST(params) AS p) "
            "SELECT k FROM x",
            {("params", "key")},
        ),
        (
            # EXISTS needs no output columns, so only the condition's field is read.
            "SELECT user_id FROM events "
            "WHERE EXISTS (SELECT value FROM UNNEST(params) WHERE key = 'a')",
            {("params", "key"), ("user_id",)},
        ),
    ],
)
def test_struct_paths(sql: str, paths: set[tuple[str, ...]] | None) -> None:
    (events,) = [t for t in facts_for(sql).tables if t.table.name == "events"]
    assert events.paths == paths


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
    facts = facts_for(
        "INSERT INTO `proj.analytics.users` (user_id) SELECT user_id FROM `proj.analytics.events`"
    )
    assert [t.table.name for t in facts.tables] == ["events"]


def test_non_query_is_rejected() -> None:
    with pytest.raises(FactsError, match="DROP TABLE statements have no query"):
        facts_for("DROP TABLE `proj.analytics.users`")


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


# Issue #6: filters and columns follow CTEs, subqueries and UNION branches.


def test_filter_on_cte_column_reaches_the_table() -> None:
    facts = facts_for(
        "WITH b AS (SELECT user_id, event_date FROM events) "
        "SELECT user_id FROM b WHERE event_date = '2026-09-01'"
    )
    predicate = only_predicate(table(facts, "events"))
    assert (predicate.column, predicate.op, predicate.values) == (
        "event_date",
        "=",
        ("'2026-09-01'",),
    )
    assert predicate.sql == "`events`.`event_date` = '2026-09-01'"


def test_filter_on_derived_table_column_reaches_the_table() -> None:
    facts = facts_for(
        "SELECT user_id FROM (SELECT user_id, event_date AS day FROM events) "
        "WHERE day >= '2026-09-01'"
    )
    predicate = only_predicate(table(facts, "events"))
    assert (predicate.column, predicate.op) == ("event_date", ">=")


def test_filter_through_nested_ctes() -> None:
    facts = facts_for(
        "WITH a AS (SELECT * FROM events), b AS (SELECT user_id, event_date FROM a) "
        "SELECT user_id FROM b WHERE event_date = '2026-09-01'"
    )
    assert only_predicate(table(facts, "events")).column == "event_date"


def test_filter_on_computed_cte_column_keeps_its_wrapper() -> None:
    facts = facts_for(
        "WITH b AS (SELECT DATE(event_ts) AS day, user_id FROM events) "
        "SELECT user_id FROM b WHERE day = '2026-09-01'"
    )
    predicate = only_predicate(table(facts, "events"))
    assert (predicate.column, predicate.wrapper) == ("event_ts", "DATE")


def test_filter_on_group_key_passes_group_by() -> None:
    facts = facts_for(
        "WITH daily AS (SELECT event_date, COUNT(*) AS n FROM events GROUP BY event_date) "
        "SELECT * FROM daily WHERE event_date = '2026-09-01'"
    )
    assert only_predicate(table(facts, "events")).column == "event_date"


@pytest.mark.parametrize(
    "cte",
    [
        "SELECT user_id, COUNT(*) AS n FROM events GROUP BY user_id",
        "SELECT user_id, ROW_NUMBER() OVER (ORDER BY event_ts) AS n FROM events",
        "SELECT user_id, (SELECT COUNT(*) FROM users) AS n FROM events",
    ],
)
def test_filter_on_aggregate_window_or_subquery_stays_above(cte: str) -> None:
    facts = facts_for(f"WITH b AS ({cte}) SELECT user_id FROM b WHERE n > 1")
    assert table(facts, "events").predicates == ()


@pytest.mark.parametrize("clause", ["LIMIT 10", "QUALIFY ROW_NUMBER() OVER () = 1"])
def test_filter_does_not_pass_limit_or_qualify(clause: str) -> None:
    facts = facts_for(
        f"WITH b AS (SELECT user_id, event_date FROM events {clause}) "
        "SELECT user_id FROM b WHERE event_date = '2026-09-01'"
    )
    assert table(facts, "events").predicates == ()


def test_cte_referenced_twice_with_different_filters() -> None:
    facts = facts_for(
        "WITH x AS (SELECT user_id, event_date FROM events) "
        "SELECT a.user_id FROM x a JOIN x b ON a.user_id = b.user_id "
        "WHERE a.event_date = '2026-09-01' AND b.event_date = '2026-09-02'"
    )
    events = [t for t in facts.tables if t.table.name == "events"]
    assert sorted(only_predicate(t).values for t in events) == [
        ("'2026-09-01'",),
        ("'2026-09-02'",),
    ]
    assert [t.scans for t in events] == [1, 1]


def test_filter_reaches_every_union_branch_by_position() -> None:
    facts = facts_for(
        "WITH all_events AS ("
        "SELECT user_id, event_date FROM events "
        "UNION ALL SELECT user_id AS uid, event_date AS d FROM events_archive) "
        "SELECT user_id FROM all_events WHERE event_date = '2026-09-01'"
    )
    assert only_predicate(table(facts, "events")).column == "event_date"
    assert only_predicate(table(facts, "events_archive")).column == "event_date"


def test_filter_does_not_pass_a_limited_union() -> None:
    facts = facts_for(
        "SELECT user_id FROM (SELECT user_id, event_date FROM events "
        "UNION ALL SELECT user_id, event_date FROM events_archive LIMIT 5) "
        "WHERE event_date = '2026-09-01'"
    )
    assert all(t.predicates == () for t in facts.tables)


def test_star_in_cte_reads_only_what_the_reader_uses() -> None:
    facts = facts_for("WITH b AS (SELECT * FROM events) SELECT user_id FROM b")
    events = table(facts, "events")
    assert events.star
    assert events.columns == {"user_id"}


def test_star_in_cte_with_filter_reads_filter_column() -> None:
    facts = facts_for(
        "WITH b AS (SELECT * FROM events) SELECT user_id FROM b WHERE event_date = '2026-09-01'"
    )
    events = table(facts, "events")
    assert events.columns == {"user_id", "event_date"}
    assert only_predicate(events).column == "event_date"


def test_star_in_derived_table() -> None:
    facts = facts_for("SELECT country FROM (SELECT * FROM users)")
    assert table(facts, "users").columns == {"country"}


def test_count_star_over_cte_reads_no_projection_columns() -> None:
    facts = facts_for("WITH b AS (SELECT user_id, country FROM users) SELECT COUNT(*) AS n FROM b")
    assert table(facts, "users").columns == frozenset()


def test_union_all_reads_only_used_positions() -> None:
    facts = facts_for(
        "WITH u AS (SELECT user_id, event_name FROM events "
        "UNION ALL SELECT user_id, event_date FROM events_archive) SELECT user_id FROM u"
    )
    assert table(facts, "events").columns == {"user_id"}
    assert table(facts, "events_archive").columns == {"user_id"}


def test_union_distinct_reads_every_column() -> None:
    facts = facts_for(
        "WITH u AS (SELECT user_id, event_name FROM events "
        "UNION DISTINCT SELECT user_id, CAST(event_date AS STRING) FROM events_archive) "
        "SELECT user_id FROM u"
    )
    assert table(facts, "events").columns == {"user_id", "event_name"}


@pytest.mark.parametrize(
    "inner",
    [
        "SELECT DISTINCT user_id, country FROM users",
        "SELECT country, COUNT(*) AS n FROM users GROUP BY country ORDER BY n",
    ],
)
def test_distinct_and_alias_references_keep_their_columns(inner: str) -> None:
    facts = facts_for(f"SELECT country FROM ({inner})")
    assert "country" in table(facts, "users").columns
    if "DISTINCT" in inner:
        assert table(facts, "users").columns == {"user_id", "country"}


def test_group_by_output_alias_reads_its_column() -> None:
    facts = facts_for(
        "WITH d AS (SELECT DATE(event_ts) AS day, COUNT(*) AS n FROM events GROUP BY 1) "
        "SELECT n FROM d"
    )
    assert table(facts, "events").columns == {"event_ts"}


def test_unused_correlated_projection_reads_nothing() -> None:
    facts = facts_for(
        "WITH b AS (SELECT u.country, (SELECT MAX(e.event_date) FROM events e "
        "WHERE e.user_id = u.user_id) AS last_seen FROM users u) SELECT country FROM b"
    )
    assert table(facts, "u").columns == {"country"}


# Issue #6: pseudo-columns in joins.


def test_unqualified_table_suffix_in_join_belongs_to_the_wildcard_table() -> None:
    facts = facts_for(
        "SELECT g.event_name FROM `ga4.events_*` g JOIN users u ON g.user_pseudo_id = u.user_id "
        "WHERE _TABLE_SUFFIX > '20260901'"
    )
    predicate = only_predicate(table(facts, "g"))
    assert (predicate.column, predicate.op) == ("_table_suffix", ">")
    assert table(facts, "u").predicates == ()
    assert table(facts, "g").columns == {"event_name", "user_pseudo_id"}


# Issue #6: a WHERE condition links a join only to sources before it.


def test_where_condition_links_only_earlier_sources() -> None:
    facts = facts_for(
        "SELECT a.user_id FROM events a, users b, events_archive c WHERE b.user_id = c.user_id"
    )
    to_b, to_c = facts.joins
    assert (to_b.target, to_b.has_condition, to_b.keys) == ("b", False, ())
    assert (to_c.target, to_c.has_condition, to_c.keys) == (
        "c",
        True,
        (("b.user_id", "c.user_id"),),
    )


def test_non_equality_where_condition_links_a_join() -> None:
    facts = facts_for(
        "SELECT e.user_id FROM events e, plans p "
        "WHERE e.event_date BETWEEN p.valid_from AND p.valid_to"
    )
    (join,) = facts.joins
    assert join.has_condition
    assert join.keys == ()


# Outer joins: ON filters only the side that isn't preserved.


@pytest.mark.parametrize(
    ("join", "events_filtered", "users_filtered"),
    [
        ("JOIN", True, True),
        ("LEFT JOIN", False, True),
        ("RIGHT JOIN", True, False),
        ("FULL JOIN", False, False),
    ],
)
def test_on_filters_follow_join_side(
    join: str, events_filtered: bool, users_filtered: bool
) -> None:
    facts = facts_for(
        f"SELECT e.user_id FROM events e {join} users u "
        "ON e.user_id = u.user_id AND e.event_date = '2026-09-01' AND u.country = 'US'"
    )
    assert bool(table(facts, "e").predicates) is events_filtered
    assert bool(table(facts, "u").predicates) is users_filtered


# Issue #6: one entry point that owns parsing and resolution.


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT nope FROM events", "Column `nope` does not exist"),
        ("SELECT FROM WHERE", "could not be parsed"),
        ("SELECT 1; SELECT 2", "expected one statement, found 2"),
        ("SELECT * FROM missing_table", "Table `missing_table` does not exist"),
    ],
)
def test_statements_that_do_not_resolve_are_rejected(sql: str, message: str) -> None:
    with pytest.raises(FactsError, match=message):
        facts_for(sql)


def test_information_schema_has_no_table_facts() -> None:
    facts = facts_for("SELECT table_name FROM analytics.INFORMATION_SCHEMA.TABLES")
    assert facts.tables == ()


def test_cte_read_twice_per_level_is_visited_once_per_level() -> None:
    ctes = ["c0 AS (SELECT user_id FROM users)"] + [
        f"c{i} AS (SELECT a.user_id FROM c{i - 1} a JOIN c{i - 1} b USING (user_id))"
        for i in range(1, 12)
    ]
    facts = facts_for(f"WITH {', '.join(ctes)} SELECT user_id FROM c11")
    assert table(facts, "users").scans == 2**11


def test_too_many_ways_to_read_a_cte_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(facts_module, "_MAX_REVISITS", 0)
    with pytest.raises(FactsError, match="too many ways"):
        facts_for(
            "WITH x AS (SELECT user_id, event_date FROM events) "
            "SELECT a.user_id FROM x a JOIN x b USING (user_id) "
            "WHERE a.event_date = '2026-09-01' AND b.event_date = '2026-09-02'"
        )


# Code review of #6.


def test_filter_does_not_pass_a_window_over_other_columns() -> None:
    facts = facts_for(
        "SELECT * FROM (SELECT event_date, SUM(1) OVER (ORDER BY event_ts) AS running "
        "FROM events) WHERE event_date = '2026-09-01'"
    )
    assert table(facts, "events").predicates == ()


def test_filter_on_window_partition_key_passes_the_window() -> None:
    facts = facts_for(
        "SELECT * FROM (SELECT event_date, user_id, "
        "ROW_NUMBER() OVER (PARTITION BY event_date ORDER BY event_ts) AS rn FROM events) "
        "WHERE event_date = '2026-09-01' AND user_id = 'u1'"
    )
    assert [p.column for p in table(facts, "events").predicates] == ["event_date"]


def test_filter_does_not_enter_a_recursive_cte() -> None:
    facts = facts_for(
        "WITH RECURSIVE r AS (SELECT event_date AS d FROM events WHERE user_id = 'a' "
        "UNION ALL SELECT DATE_ADD(r.d, INTERVAL 1 DAY) AS d FROM r WHERE r.d < '2026-02-01') "
        "SELECT * FROM r WHERE d = '2026-01-15'"
    )
    events = table(facts, "events")
    assert [p.column for p in events.predicates] == ["user_id"]
    assert events.scans == 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x FROM UNNEST((SELECT ARRAY_AGG(user_id) FROM users)) AS x",
        "SELECT e.user_id FROM events e "
        "CROSS JOIN UNNEST((SELECT ARRAY_AGG(country) FROM users)) AS c",
    ],
)
def test_subquery_inside_unnest_is_read(sql: str) -> None:
    assert "users" in [t.table.name for t in facts_for(sql).tables]


def test_long_union_all_chain_does_not_recurse() -> None:
    branches = " UNION ALL ".join(
        f"SELECT user_id FROM events WHERE event_date = '2026-01-{i % 28 + 1:02d}'"
        for i in range(600)
    )
    facts = facts_for(branches)
    assert sum(t.scans for t in facts.tables) == 600


def test_export_data_reports_the_exported_query() -> None:
    facts = facts_for(
        "EXPORT DATA OPTIONS (uri = 'gs://b/*.csv', format = 'CSV') AS "
        "SELECT user_id FROM events WHERE event_date = '2026-09-01'"
    )
    assert only_predicate(table(facts, "events")).column == "event_date"


@pytest.mark.parametrize(
    ("condition", "filtered"),
    [
        ("u.signup_date IS NULL", False),
        ("COALESCE(u.country, 'US') = 'US'", False),
        ("u.country = 'US'", True),
    ],
)
def test_where_on_outer_joined_side_counts_only_if_it_rejects_nulls(
    condition: str, filtered: bool
) -> None:
    facts = facts_for(
        "SELECT e.user_id FROM events e LEFT JOIN (SELECT user_id, country, signup_date "
        f"FROM users) u ON e.user_id = u.user_id WHERE {condition}"
    )
    assert bool(table(facts, "users").predicates) is filtered


def test_subquery_in_unread_output_column_is_not_scanned() -> None:
    facts = facts_for(
        "WITH c AS (SELECT user_id, (SELECT MAX(signup_date) FROM users) AS m FROM events) "
        "SELECT user_id FROM c"
    )
    assert [t.table.name for t in facts.tables] == ["events"]


@pytest.mark.parametrize(
    ("sql", "alias"),
    [
        ("SELECT TO_JSON_STRING(t) AS j FROM (SELECT user_id, event_date FROM events) t", "events"),
        ("WITH b AS (SELECT user_id, event_date FROM events) SELECT b FROM b", "events"),
        ("SELECT ARRAY_AGG(e) AS rows FROM events e", "e"),
    ],
)
def test_whole_row_reference_reads_every_column(sql: str, alias: str) -> None:
    columns = table(facts_for(sql), alias).columns
    assert {"user_id", "event_date"} <= columns
    if alias == "e":
        assert columns == {c.name for c in CATALOG.tables[0].columns}


@pytest.mark.parametrize("condition", ["d.country IS NULL", "COALESCE(d.country, 'x') = 'x'"])
def test_reader_on_filter_skips_inner_outer_join_nulls(condition: str) -> None:
    facts = facts_for(
        "SELECT e.user_id FROM events e LEFT JOIN (SELECT ev.event_date, u2.country "
        "FROM events ev LEFT JOIN users u2 ON ev.user_id = u2.user_id) d "
        f"ON e.event_date = d.event_date AND {condition}"
    )
    assert table(facts, "u2").predicates == ()


# Code review of PR #8.


@pytest.mark.parametrize(
    "sql",
    [
        "(SELECT user_id FROM events WHERE event_date = '2026-09-01')",
        "(SELECT user_id FROM events WHERE event_date = '2026-09-01') LIMIT 5",
    ],
)
def test_query_in_parentheses(sql: str) -> None:
    assert only_predicate(table(facts_for(sql), "events")).column == "event_date"


def test_update_has_no_facts() -> None:
    with pytest.raises(FactsError, match="UPDATE statements have no query"):
        facts_for(
            "UPDATE users SET country = (SELECT MAX(event_name) FROM events) "
            "WHERE user_id IN (SELECT user_id FROM events_archive)"
        )


@pytest.mark.parametrize(
    "aggregate", ["`proj.analytics.mode_agg`(event_date)", "HLL_COUNT.INIT(event_date)"]
)
def test_filter_does_not_pass_an_unknown_aggregate(aggregate: str) -> None:
    facts = facts_for(
        f"SELECT * FROM (SELECT user_id, {aggregate} AS d FROM events GROUP BY user_id) "
        "WHERE d = '2026-09-01'"
    )
    assert table(facts, "events").predicates == ()


def test_filter_on_non_key_column_does_not_pass_group_by() -> None:
    facts = facts_for(
        "SELECT * FROM (SELECT user_id, ANY_VALUE(event_date) AS d FROM events GROUP BY user_id) "
        "WHERE d = '2026-09-01'"
    )
    assert table(facts, "events").predicates == ()


def test_filter_does_not_pass_rollup() -> None:
    facts = facts_for(
        "SELECT * FROM (SELECT event_date AS d, COUNT(*) AS n FROM events "
        "GROUP BY ROLLUP (event_date)) WHERE d IS NULL"
    )
    assert table(facts, "events").predicates == ()


@pytest.mark.parametrize(
    "source",
    [
        "((SELECT user_id, event_date AS d FROM events) LIMIT 5)",
        "((SELECT user_id, event_date AS d FROM events "
        "UNION ALL SELECT user_id, event_date FROM events_archive) LIMIT 5)",
    ],
)
def test_filter_does_not_pass_limit_on_parentheses(source: str) -> None:
    facts = facts_for(f"SELECT * FROM {source} WHERE d = '2026-09-01'")
    assert all(t.predicates == () for t in facts.tables)


def test_union_by_name_matches_columns_by_name() -> None:
    union = (
        "(SELECT user_id AS a, country AS b FROM users "
        "UNION ALL BY NAME SELECT country AS b, user_id AS a FROM users)"
    )
    filtered = facts_for(f"SELECT * FROM {union} WHERE a = 'x'")
    assert {p.column for t in filtered.tables for p in t.predicates} == {"user_id"}
    pruned = facts_for(f"SELECT b FROM {union}")
    assert {c for t in pruned.tables for c in t.columns} == {"country"}


def test_union_order_by_reads_its_column_in_every_branch() -> None:
    facts = facts_for(
        "SELECT a FROM (SELECT user_id AS a, event_date AS b FROM events "
        "UNION ALL SELECT user_id, event_date FROM events_archive ORDER BY b LIMIT 10)"
    )
    assert all(t.columns == {"user_id", "event_date"} for t in facts.tables)


@pytest.mark.parametrize("connector", [" AND ", " OR "])
def test_long_condition_chains_do_not_recurse(connector: str) -> None:
    condition = connector.join(f"user_id != 'u{i}'" for i in range(1_200))
    facts = facts_for(f"SELECT user_id FROM events WHERE {condition}")
    assert table(facts, "events").columns == {"user_id"}


def test_filters_differing_only_in_literal_type_stay_apart() -> None:
    facts = facts_for(
        "WITH c AS (SELECT user_id FROM users) "
        "SELECT * FROM c WHERE CAST(user_id AS BYTES) = b'a' "
        "UNION ALL SELECT * FROM c WHERE CAST(user_id AS BYTES) = b'b'"
    )
    users = [t for t in facts.tables if t.table.name == "users"]
    assert len(users) == 2


def test_plain_cte_in_with_recursive_still_takes_filters() -> None:
    facts = facts_for(
        "WITH RECURSIVE a AS (SELECT * FROM events) "
        "SELECT user_id FROM a WHERE event_date = '2026-09-01'"
    )
    events = table(facts, "events")
    assert only_predicate(events).column == "event_date"
    assert events.columns == {"user_id", "event_date"}


def test_star_over_duplicate_names_reads_the_columns() -> None:
    facts = facts_for("SELECT * FROM (SELECT u.country, u.country FROM users u)")
    assert table(facts, "u").columns == {"country"}


def test_inner_join_on_does_not_filter_an_outer_joined_side_with_is_null() -> None:
    facts = facts_for(
        "SELECT e.user_id FROM events e LEFT JOIN users u ON e.user_id = u.user_id "
        "JOIN events_archive a ON a.user_id = e.user_id AND u.signup_date IS NULL"
    )
    assert table(facts, "u").predicates == ()


def aliases(facts: QueryFacts) -> list[list[list[str]]]:
    return [[[member.alias for member in group] for group in p.groups] for p in facts.products]


@pytest.mark.parametrize(
    ("sql", "groups"),
    [
        ("SELECT 1 FROM events AS e JOIN users AS u ON u.user_id = e.user_id", []),
        ("SELECT 1 FROM events AS e, users AS u", [[["e"], ["u"]]]),
        ("SELECT 1 FROM events AS e JOIN users AS u ON TRUE", [[["e"], ["u"]]]),
        ("SELECT 1 FROM events AS e JOIN users AS u ON u.country = 'NL'", [[["e"], ["u"]]]),
        # A later condition connects the first two, as BigQuery reorders joins.
        (
            "SELECT 1 FROM events AS e, users AS u, events_archive AS a "
            "WHERE a.user_id = e.user_id AND a.user_id = u.user_id",
            [],
        ),
        ("SELECT 1 FROM events AS e JOIN users AS u USING (user_id)", []),
        (
            "SELECT 1 FROM events AS e "
            "JOIN users AS u ON e.user_id = u.user_id OR u.country = e.event_name",
            [],
        ),
        # HAVING and QUALIFY run after the join, so they connect nothing.
        (
            "SELECT e.user_id FROM events AS e, users AS u "
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY e.user_id ORDER BY u.signup_date) = 1",
            [[["e"], ["u"]]],
        ),
        # A condition on an UNNEST of a source's array connects that source.
        (
            "SELECT 1 FROM events AS e, UNNEST(e.params) AS p, users AS u "
            "WHERE p.value = u.user_id",
            [],
        ),
        ("SELECT 1 FROM events AS e, UNNEST(e.tags) AS t", []),
        ("SELECT 1 FROM users AS u CROSS JOIN (SELECT MAX(event_date) AS d FROM events)", []),
        ("SELECT 1 FROM users AS u CROSS JOIN (SELECT CURRENT_DATE() AS d)", []),
        (
            "WITH r AS (SELECT user_id FROM events) SELECT 1 FROM r JOIN users AS u ON TRUE",
            [[["r"], ["u"]]],
        ),
        (
            "SELECT 1 FROM events AS e JOIN users AS u ON u.user_id = e.user_id, plans AS p",
            [[["e", "u"], ["p"]]],
        ),
    ],
)
def test_products(sql: str, groups: list[list[list[str]]]) -> None:
    assert aliases(facts_for(sql)) == groups


def test_product_records_the_inequality_and_where_it_starts() -> None:
    facts = facts_for(
        "SELECT 1\nFROM events AS e\n"
        "JOIN plans AS p ON e.event_date BETWEEN p.valid_from AND p.valid_to"
    )
    (product,) = facts.products
    assert product.inequality == "e.event_date BETWEEN p.valid_from AND p.valid_to"
    assert product.position == (3, 6)
    assert ("p", "valid_from") in product.relating


def test_derived_source_bounds() -> None:
    facts = facts_for(
        "SELECT 1 FROM users AS u, (SELECT user_id FROM events LIMIT 5) AS a, "
        "(SELECT 'x' AS k UNION ALL SELECT 'y') AS b"
    )
    (product,) = facts.products
    derived = {m.alias: m.rows for g in product.groups for m in g if isinstance(m, DerivedSource)}
    assert derived == {"a": 5, "b": 2}
