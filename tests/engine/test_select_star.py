"""Edge cases for SCN005; tests/golden/scn005 covers the main behavior."""

from datetime import UTC, datetime

import pytest

from scanisaur.catalog import Catalog, Column, Partition, Partitioning, Table
from scanisaur.engine.check import Policy, check
from scanisaur.engine.result import Severity
from scanisaur.engine.rules import SELECT_STAR
from scanisaur.engine.select_star import LARGE_BYTES

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
COLUMNS = (Column("a", "INT64"), Column("b", "INT64"))


def star_findings(sql: str, *tables: Table, policy: Policy | None = None) -> list[str]:
    result = check(sql, Catalog(tables, "p", "d"), now=NOW, policy=policy or Policy())
    return [f.message for f in result.findings if f.rule == SELECT_STAR]


def table(size: int | None, *, kind: str = "TABLE", name: str = "t") -> Table:
    rows = None if size is None else size // 16
    return Table("p", "d", name, COLUMNS, kind=kind, row_count=rows, size_bytes=size)  # type: ignore[arg-type]


class TestSelectStar:
    def test_unknown_size_warns_without_an_amount(self) -> None:
        (message,) = star_findings("SELECT * FROM t", table(None))
        assert message == "Selecting `*` reads every column of `p.d.t`."

    @pytest.mark.parametrize(("size", "warns"), [(LARGE_BYTES, True), (LARGE_BYTES - 2**20, False)])
    def test_size_threshold(self, size: int, warns: bool) -> None:
        assert bool(star_findings("SELECT * FROM t", table(size))) is warns

    def test_view_is_not_reported(self) -> None:
        assert star_findings("SELECT * FROM t", table(None, kind="VIEW")) == []

    def test_one_finding_per_table(self) -> None:
        sql = "SELECT * FROM t AS x JOIN t AS y USING (a)"
        assert len(star_findings(sql, table(4 * LARGE_BYTES))) == 1

    def test_each_large_table_is_reported(self) -> None:
        sql = "SELECT * FROM t JOIN s USING (a)"
        tables = (table(4 * LARGE_BYTES), table(4 * LARGE_BYTES, name="s"))
        assert len(star_findings(sql, *tables)) == 2

    def test_external_table_is_not_reported(self) -> None:
        assert star_findings("SELECT * FROM t", table(None, kind="EXTERNAL")) == []

    def test_union_of_small_reads_adds_up(self) -> None:
        # Each branch reads 0.6 GiB, under the threshold; together they read 1.2 GiB.
        half = Table(
            "p",
            "d",
            "t",
            (Column("d", "DATE"), Column("a", "INT64")),
            row_count=LARGE_BYTES * 6 // 160,
            size_bytes=LARGE_BYTES * 6 // 10,
            partitioning=Partitioning("d", "DAY"),
            partitions=(Partition("20260901", LARGE_BYTES * 6 // 10),),
        )
        two = Table(
            "p",
            "d",
            "t",
            half.columns,
            row_count=half.row_count * 2,  # type: ignore[operator]
            size_bytes=half.size_bytes * 2,  # type: ignore[operator]
            partitioning=half.partitioning,
            partitions=(*half.partitions, Partition("20260902", LARGE_BYTES * 6 // 10)),
        )
        sql = (
            "SELECT * FROM t WHERE d = '2026-09-01' "
            "UNION ALL SELECT * FROM t WHERE d = '2026-09-02'"
        )
        (message,) = star_findings(sql, two)
        assert message.endswith("in the partitions the query touches, about 1.3 GB.")

    def test_clustered_table_gives_an_upper_bound(self) -> None:
        clustered = Table(
            "p",
            "d",
            "t",
            COLUMNS,
            row_count=4 * LARGE_BYTES // 16,
            size_bytes=4 * LARGE_BYTES,
            clustering=("a",),
        )
        (message,) = star_findings("SELECT * FROM t LIMIT 5", clustered)
        assert message == "Selecting `*` reads every column of `p.d.t`, up to 4.3 GB."

    def test_insert_select_star_reads_every_column(self) -> None:
        sql = "INSERT INTO s (a, b) SELECT * FROM t"
        tables = (table(4 * LARGE_BYTES), table(0, name="s"))
        (message,) = star_findings(sql, *tables, policy=Policy(read_only=False))
        assert message.startswith("Selecting `*` reads every column of `p.d.t`")

    def test_warns_and_names_the_fix(self) -> None:
        result = check(
            "SELECT * FROM t LIMIT 3", Catalog((table(4 * LARGE_BYTES),), "p", "d"), now=NOW
        )
        (finding,) = result.findings
        assert (finding.rule, finding.severity) == (SELECT_STAR, Severity.WARN)
        assert finding.message == (
            "`LIMIT 3` doesn't reduce the bytes billed: selecting `*` reads every column of "
            "`p.d.t`, about 4.3 GB."
        )
        assert finding.fix is not None
        assert "scanisaur_schema_describe" in finding.fix
