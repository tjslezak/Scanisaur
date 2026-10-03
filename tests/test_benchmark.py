"""The dry-run benchmark's harness (benchmark/run.py), without the network."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from benchmark import run

from scanisaur.catalog.fixtures import load_catalog
from scanisaur.engine.estimate import MIN_BILLED_BYTES
from scanisaur.engine.parse import DIALECT, parse

NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)


class TestQueries:
    def test_every_query_parses_and_expects_known_rules(self) -> None:
        queries = run.load_queries(run.QUERIES)
        assert len(queries) >= 50
        for query in queries:
            assert len(parse(query.sql, DIALECT)) == 1, query.id
            assert set(query.expect) <= set(run.RULES), query.id

    def test_duplicate_ids_are_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "queries.yaml"
        path.write_text("- {id: a, sql: SELECT 1}\n- {id: a, sql: SELECT 2}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="more than once"):
            run.load_queries(path)

    def test_missing_sql_names_the_query(self, tmp_path: Path) -> None:
        path = tmp_path / "queries.yaml"
        path.write_text("- {id: a}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="query a needs a string id and sql"):
            run.load_queries(path)


class TestDryRunOutput:
    def test_json_job(self) -> None:
        job = {"statistics": {"query": {"totalBytesProcessed": "46822020"}}}
        assert run.parse_dry_run(json.dumps(job)) == 46_822_020

    def test_sentence(self) -> None:
        out = (
            "Query successfully validated. ... running this query will process 1024 bytes of data."
        )
        assert run.parse_dry_run(out) == 1024

    @pytest.mark.parametrize("out", ["{}", "nothing useful"])
    def test_unreadable(self, out: str) -> None:
        with pytest.raises(run.BqError):
            run.parse_dry_run(out)

    def test_bq_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(*_args: Any, **_kwargs: Any) -> Any:
            class Result:
                returncode = 1
                stdout = ""
                stderr = "Error in query string: Cannot query over table without a filter"

            return Result()

        monkeypatch.setattr("subprocess.run", fail)
        with pytest.raises(run.BqError, match="without a filter"):
            run.bq_dry_run("p", "SELECT 1")

    def test_wrapped_error_is_kept_whole(self) -> None:
        stderr = (
            "BigQuery error in query operation: Error processing job 'p:bqjob_r1': Cannot query\n"
            "over table 'w.pageviews' without a filter over column(s) 'datehour' that\n"
            "can be used for partition elimination\n"
        )
        assert run.error_message(stderr, 1) == (
            "Cannot query over table 'w.pageviews' without a filter over column(s) 'datehour' "
            "that can be used for partition elimination"
        )
        assert run.error_message("", 2) == "bq exited with 2"


ROWS: dict[str, list[dict[str, Any]]] = {
    "__TABLES__ WHERE table_id = 't'": [
        {"table_id": "t", "row_count": "100", "size_bytes": "1600"}
    ],
    "COLUMNS WHERE table_name = 't'": [
        {
            "column_name": "day",
            "data_type": "DATE",
            "is_partitioning_column": "YES",
            "clustering_ordinal_position": None,
        },
        {
            "column_name": "k",
            "data_type": "STRING",
            "is_partitioning_column": "NO",
            "clustering_ordinal_position": "1",
        },
        {
            "column_name": "odd",
            "data_type": "NOT A TYPE",
            "is_partitioning_column": "NO",
            "clustering_ordinal_position": None,
        },
    ],
    "PARTITIONS WHERE table_name = 't'": [
        {"partition_id": "20260930", "total_logical_bytes": "800"},
        {"partition_id": "__NULL__", "total_logical_bytes": "800"},
    ],
    "TABLE_OPTIONS WHERE table_name = 't'": [{"option_value": "true"}],
    "__TABLES__ WHERE STARTS_WITH(table_id, 'e_')": [
        {"table_id": "e_20260929", "row_count": "1", "size_bytes": "10"},
        {"table_id": "e_20260930", "row_count": "2", "size_bytes": "20"},
    ],
    "COLUMNS WHERE table_name = 'e_20260930'": [
        {
            "column_name": "n",
            "data_type": "INT64",
            "is_partitioning_column": "NO",
            "clustering_ordinal_position": None,
        },
    ],
}


class TestSnapshot:
    def fake_rows(self, _project: str, sql: str) -> list[dict[str, Any]]:
        for key, rows in ROWS.items():
            view, condition = key.split(" ", 1)
            if view in sql and condition in sql:
                return rows
        return []

    def test_snapshot_loads_as_a_catalog(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(run, "bq_rows", self.fake_rows)
        spec = run.snapshot("p", ["o.d.t", "o.d.e_*"])
        path = tmp_path / "catalog.yaml"
        run.write_catalog(spec, NOW, path)
        table, shards = load_catalog(path).tables
        assert table.partitioning is not None
        assert (table.partitioning.column, table.partitioning.granularity) == ("day", "DAY")
        assert table.partitioning.required
        assert table.clustering == ("k",)
        assert [c.type for c in table.columns] == ["DATE", "STRING", "JSON"]
        assert {p.id: p.size_bytes for p in table.partitions} == {"20260930": 800, "__NULL__": 800}
        assert (shards.row_count, shards.size_bytes) == (3, 30)
        assert {p.id for p in shards.partitions} == {"20260929", "20260930"}

    def test_missing_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run, "bq_rows", lambda _p, _s: [])
        with pytest.raises(run.BqError, match="doesn't exist"):
            run.snapshot("p", ["o.d.missing"])

    def test_unpartitioned_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = {
            "__TABLES__": [{"table_id": "u", "row_count": "1", "size_bytes": "8"}],
            "COLUMNS": [
                {
                    "column_name": "a",
                    "data_type": "INT64",
                    "is_partitioning_column": "NO",
                    "clustering_ordinal_position": None,
                }
            ],
            "PARTITIONS": [{"partition_id": None, "total_logical_bytes": "8"}],
        }
        monkeypatch.setattr(
            run, "bq_rows", lambda _p, sql: next(v for k, v in rows.items() if k in sql)
        )
        (spec,) = run.snapshot("p", ["o.d.u"])["tables"]
        assert "partitioning" not in spec

    def test_refresh_writes_both_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "tables.yaml").write_text("tables: [o.d.t]\n", encoding="utf-8")
        (tmp_path / "queries.yaml").write_text(
            "- {id: ok, sql: SELECT k FROM `o.d.t`}\n- {id: bad, sql: SELECT nope FROM `o.d.t`}\n",
            encoding="utf-8",
        )
        for name in ("TABLES", "QUERIES", "CATALOG", "DRY_RUNS"):
            target = (
                tmp_path
                / {
                    "TABLES": "tables.yaml",
                    "QUERIES": "queries.yaml",
                    "CATALOG": "catalog.yaml",
                    "DRY_RUNS": "dry_runs.json",
                }[name]
            )
            monkeypatch.setattr(run, name, target)
        monkeypatch.setattr(run, "bq_rows", self.fake_rows)

        def dry_run(_project: str, sql: str) -> int:
            if "nope" in sql:
                raise run.BqError("Unrecognized name: nope")
            return 5

        monkeypatch.setattr(run, "bq_dry_run", dry_run)
        run.refresh("p")
        runs = run.load_dry_runs(tmp_path / "dry_runs.json")
        assert (runs.bytes, runs.errors) == ({"ok": 5}, {"bad": "Unrecognized name: nope"})
        assert load_catalog(tmp_path / "catalog.yaml").tables[0].name == "t"

        # An interrupted refresh leaves both files as they were.
        before = {name: (tmp_path / name).read_text() for name in ("catalog.yaml", "dry_runs.json")}

        def interrupted(_project: str, _sql: str) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(run, "bq_dry_run", interrupted)
        with pytest.raises(KeyboardInterrupt):
            run.refresh("p")
        assert {name: (tmp_path / name).read_text() for name in before} == before
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "catalog.yaml",
            "dry_runs.json",
            "queries.yaml",
            "tables.yaml",
        ]


class TestReport:
    CATALOG = """
tables:
  - name: o.d.t
    rows: 1073741824
    bytes: 17179869184
    partitioning: {column: day, granularity: DAY, required: true}
    columns: {day: DATE, n: INT64}
    partitions: {'20260929': 8589934592, '20260930': 8589934592}
"""
    SMALL = """  - name: o.d.s
    rows: 10
    bytes: 80
    columns: {n: INT64}
"""

    def outcomes(
        self, tmp_path: Path, bytes_: dict[str, int], errors: dict[str, str]
    ) -> list[run.Outcome]:
        path = tmp_path / "catalog.yaml"
        path.write_text(self.CATALOG, encoding="utf-8")
        queries = [
            # A LIMIT keeps SCN009 quiet; the table isn't clustered, so it bills the same.
            run.Query("one-day", "SELECT n FROM `o.d.t` WHERE day = '2026-09-30' LIMIT 10"),
            run.Query("all", "SELECT n FROM `o.d.t`", ("SCN003",)),
            run.Query("count", "SELECT COUNT(*) FROM `o.d.t` WHERE day = '1999-01-01'"),
            run.Query("missing", "SELECT n FROM `o.d.t` WHERE day = '2026-09-29' LIMIT 10"),
        ]
        runs = run.DryRuns(measured_at=NOW, bytes=bytes_, errors=errors)
        return run.outcomes(queries, load_catalog(path), runs)

    def test_metrics(self, tmp_path: Path) -> None:
        # One partition holds 8 GiB: `n` and the filter column `day`, 4 GiB each.
        one_day, rejected, count, missing = self.outcomes(
            tmp_path, {"one-day": 8 * 2**30, "count": 0}, {"all": "Cannot query over table"}
        )
        assert one_day.billed == (8 * 2**30, 8 * 2**30)
        assert (one_day.ratio, one_day.within_3x, one_day.in_range) == (1.0, True, True)
        assert rejected.billed is None
        assert rejected.result.estimate is None
        assert rejected.rules_match  # SCN003 blocks, as expected
        assert (count.billed, count.ratio) == ((0, 0), 1.0)
        assert (missing.error, missing.ratio) == ("not measured", None)

    def test_report_text(self, tmp_path: Path) -> None:
        results = self.outcomes(tmp_path, {"one-day": 2**30, "count": 0}, {"all": "rejected"})
        text = run.report(results, run.DryRuns(measured_at=NOW))
        assert "| **All** | 2 | 1 (50%) | 1 (50%) |" in text  # one-day is 8x over, count exact
        assert "| One value | 2 | 1 (50%) | 1 (50%) |" in text
        assert "| A range | 0 | - | - |" in text
        assert "| `one-day` | 1.1 GB | 8.6 GB (high) | 8.00 ** | none | none |" in text
        assert "| `all` | rejected: rejected | none | - | SCN003 | SCN003 |" in text
        assert "BigQuery rejected 1 queries. Scanisaur gives 1 of them no estimate" in text
        assert "Not measured yet, so left out above: `missing`." in text

    def test_rejection_scanisaur_missed(self, tmp_path: Path) -> None:
        # BigQuery rejected `one-day`, but Scanisaur estimated it: a miss to show.
        results = self.outcomes(tmp_path, {"count": 0}, {"one-day": "x", "all": "y"})
        text = run.report(results, run.DryRuns(measured_at=NOW))
        assert "BigQuery rejected 2 queries. Scanisaur gives 1 of them no estimate" in text
        assert "It estimated the others anyway: `one-day`." in text

    def test_mismatches_are_listed(self, tmp_path: Path) -> None:
        results = self.outcomes(tmp_path, {}, {})
        mislabeled = run.Outcome(
            run.Query("x", "SELECT 1", ("SCN005",)), results[0].result, None, None
        )
        text = run.report([mislabeled], run.DryRuns(measured_at=NOW))
        assert "- `x`: expected SCN005, found none" in text

    def test_query_scanisaur_cannot_check(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.yaml"
        path.write_text(self.CATALOG, encoding="utf-8")
        query = run.Query("typo", "SELECT nn FROM `o.d.t` WHERE day = '2026-09-30'")
        (outcome,) = run.outcomes([query], load_catalog(path), run.DryRuns(measured_at=NOW))
        assert outcome.found == ("SCN001",)
        assert not outcome.rules_match

    def test_billed(self) -> None:
        assert (run.billed(0), run.billed(169), run.billed(11 * 2**20 + 1)) == (
            (0, 0),
            (MIN_BILLED_BYTES, MIN_BILLED_BYTES),
            (12 * 2**20, 12 * 2**20),
        )
        # With two tables, each billed at least 10 MiB: from 7.5 + 7.5 MiB, billed 10 + 10,
        # to all 15 MiB in one, billed 15 + 10.
        assert run.billed(15 * 2**20, tables=2) == (20 * 2**20, 25 * 2**20)
        assert run.billed(25 * 2**20, tables=2) == (25 * 2**20, 35 * 2**20)
        # The join in #26 read 1.3 MB of one table and billed 20 MiB.
        assert run.billed(1_286_408, tables=2) == (2 * MIN_BILLED_BYTES, 2 * MIN_BILLED_BYTES)
        assert run.billed(0, tables=2) == (0, 0)

    def test_ratio_and_range_use_the_nearest_bill(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.yaml"
        path.write_text(self.CATALOG + self.SMALL, encoding="utf-8")
        sql = "SELECT a.n FROM `o.d.t` AS a JOIN `o.d.s` AS b USING (n) WHERE a.day = '2026-09-30'"
        query = run.Query("join", sql)
        # 4 GiB of `n` plus 4 GiB of `day` in `t`, and `s`'s minimum: the high end of the bill.
        processed = 8 * 2**30
        runs = run.DryRuns(measured_at=NOW, bytes={"join": processed})
        (outcome,) = run.outcomes([query], load_catalog(path), runs)
        assert outcome.billed == (processed, processed + MIN_BILLED_BYTES)
        assert (outcome.ratio, outcome.in_range) == (1.0, True)

    def test_report_command(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        paths = {
            name: tmp_path / name.lower() for name in ("CATALOG", "QUERIES", "DRY_RUNS", "REPORT")
        }
        paths["CATALOG"].write_text(self.CATALOG, encoding="utf-8")
        paths["QUERIES"].write_text(
            "- {id: q, sql: \"SELECT n FROM `o.d.t` WHERE day = '2026-09-30' LIMIT 10\"}\n",
            encoding="utf-8",
        )
        run.save_dry_runs(run.DryRuns(measured_at=NOW, bytes={"q": 8 * 2**30}), paths["DRY_RUNS"])
        for name, path in paths.items():
            monkeypatch.setattr(run, name, path)
        assert run.main(["report", "--write"]) == 0
        text = paths["REPORT"].read_text(encoding="utf-8")
        assert "| `q` | 8.6 GB | 8.6 GB (high) | 1.00 | none | none |" in text
