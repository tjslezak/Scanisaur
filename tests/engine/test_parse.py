import logging

import pytest
from sqlglot import exp

from scanisaur.engine.parse import SqlParseError, classify, describe, parse, position


def one(sql: str) -> exp.Expr:
    (tree,) = parse(sql, "bigquery")
    return tree


class TestParse:
    def test_statements(self) -> None:
        assert len(parse("SELECT 1; SELECT 2", "bigquery")) == 2

    def test_comments_only(self) -> None:
        assert parse("-- nothing to see", "bigquery") == []

    def test_syntax_error_points_at_the_token_start(self) -> None:
        with pytest.raises(SqlParseError) as error:
            parse("SELECT user_id\nFROM WHERE x = 1", "bigquery")
        assert error.value.message == "Expected table name but got 'WHERE'"
        assert (error.value.line, error.value.column) == (2, 6)

    def test_tokenizer_error_has_no_position(self) -> None:
        with pytest.raises(SqlParseError) as error:
            parse("SELECT 'unterminated", "bigquery")
        assert (error.value.line, error.value.column) == (None, None)

    def test_restores_sqlglot_logging(self) -> None:
        logger = logging.getLogger("sqlglot")
        before = logger.level
        parse("CALL proc()", "bigquery")
        assert logger.level == before


@pytest.mark.parametrize(
    ("sql", "kind", "name"),
    [
        ("SELECT 1", "query", "SELECT"),
        ("SELECT 1 UNION ALL SELECT 2", "query", "UNION"),
        ("(SELECT 1)", "query", "SUBQUERY"),
        ("FROM t |> SELECT a", "query", "SELECT"),
        ("INSERT INTO t (a) VALUES (1)", "write", "INSERT"),
        ("UPDATE t SET a = 1 WHERE TRUE", "write", "UPDATE"),
        ("DELETE FROM t WHERE TRUE", "write", "DELETE"),
        ("MERGE t USING s ON t.a = s.a WHEN MATCHED THEN DELETE", "write", "MERGE"),
        ("TRUNCATE TABLE t", "write", "TRUNCATE TABLE"),
        ("CREATE TABLE t AS SELECT 1 AS a", "write", "CREATE TABLE"),
        ("CREATE OR REPLACE VIEW v AS SELECT 1 AS a", "write", "CREATE VIEW"),
        ("DROP TABLE t", "write", "DROP TABLE"),
        ("ALTER TABLE t ADD COLUMN b INT64", "write", "ALTER TABLE"),
        ('GRANT `roles/bigquery.dataViewer` ON TABLE t TO "user:a@example.com"', "write", "GRANT"),
        ("EXPORT DATA OPTIONS (uri = 'gs://b/*.csv') AS SELECT 1", "write", "EXPORT DATA"),
        ("BEGIN TRANSACTION", "other", "BEGIN TRANSACTION"),
        ("DECLARE x INT64", "other", "DECLARE"),
        ("CALL proc()", "write", "CALL"),
        ("EXECUTE IMMEDIATE 'SELECT 1'", "write", "EXECUTE"),
        (
            'REVOKE `roles/bigquery.dataViewer` ON TABLE t FROM "user:a@example.com"',
            "write",
            "REVOKE",
        ),
        (
            "LOAD DATA INTO d.t FROM FILES (format = 'CSV', uris = ['gs://b/x.csv'])",
            "write",
            "LOAD DATA",
        ),
        ("CREATE SNAPSHOT TABLE d.s CLONE d.t", "write", "CREATE"),
        ("ALTER SCHEMA d SET OPTIONS (description = 'x')", "write", "ALTER"),
        ("SET x = 1", "other", "SET"),
        ("EXPLAIN SELECT 1", "other", "EXPLAIN"),
    ],
)
def test_classify_and_describe(sql: str, kind: str, name: str) -> None:
    tree = one(sql)
    assert classify(tree) == kind
    assert describe(tree) == name


class TestPosition:
    def test_column_points_at_its_name(self) -> None:
        tree = one("SELECT\n  e.user_id\nFROM events e")
        column = tree.find(exp.Column)
        assert column is not None
        assert position(column) == (2, 5)

    def test_table_points_at_its_first_part(self) -> None:
        tree = one("SELECT 1 FROM proj.analytics.events")
        table = tree.find(exp.Table)
        assert table is not None
        assert position(table) == (1, 15)

    def test_unknown_position(self) -> None:
        assert position(exp.column("a")) == (None, None)
