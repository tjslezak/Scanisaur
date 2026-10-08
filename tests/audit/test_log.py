import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scanisaur.audit.log import Decision, append, decision, log_directory, read
from scanisaur.catalog import Catalog, Column, Table
from scanisaur.config import LogSettings
from scanisaur.engine.check import check, fingerprint, shape_fingerprint
from scanisaur.engine.result import Verdict

CATALOG = Catalog(
    (Table("p", "d", "users", (Column("email", "STRING"), Column("id", "INT64"))),),
    default_project="p",
    default_dataset="d",
)
SQL = "SELECT id FROM users WHERE email = 'ana@example.com'"
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _entry(sql: str = SQL, time: datetime = NOW, *, raw_sql: bool = False) -> Decision:
    return decision(check(sql, CATALOG), sql, source="cli", now=time, raw_sql=raw_sql)


def test_entry_keeps_no_literals() -> None:
    sql = SQL + " AND emial = 1"
    entry = _entry(sql)
    line = entry.model_dump_json()
    assert "ana@example.com" not in line
    assert [f.rule for f in entry.findings] == ["SCN001"]
    assert "message" not in json.loads(line)["findings"][0]
    assert entry.query == fingerprint(sql)
    assert entry.shape == shape_fingerprint(sql)


def test_raw_sql_is_opt_in() -> None:
    assert _entry().sql is None
    assert _entry(raw_sql=True).sql == SQL


def test_same_shape_for_other_constants() -> None:
    other = _entry("SELECT id FROM users WHERE email = 'bo@example.com'")
    assert other.shape == _entry().shape
    assert other.query != _entry().query


def test_append_and_read(tmp_path: Path) -> None:
    september = _entry(time=NOW - timedelta(days=5))
    october = _entry()
    for entry in (september, october):
        append(tmp_path, entry)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09.jsonl", "2026-10.jsonl"]
    assert list(read(tmp_path, NOW - timedelta(days=10))) == [september, october]
    assert list(read(tmp_path, NOW - timedelta(days=1))) == [october]


def test_read_skips_bad_lines(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    append(tmp_path, _entry())
    with (tmp_path / "2026-10.jsonl").open("a", encoding="utf-8") as file:
        file.write("\nnot json\n")
    assert [e.verdict for e in read(tmp_path, NOW - timedelta(days=1))] == [Verdict.PASS]
    assert "2026-10.jsonl:3: not a decision log entry" in caplog.text


def test_read_skips_naive_times(tmp_path: Path) -> None:
    append(tmp_path, _entry())
    line = (tmp_path / "2026-10.jsonl").read_text(encoding="utf-8")
    naive = json.loads(line) | {"time": "2026-10-03T12:00:00"}
    with (tmp_path / "2026-10.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(naive) + "\n")
    assert len(list(read(tmp_path, NOW - timedelta(days=1)))) == 1


def test_append_rejects_partial_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scanisaur.audit.log.os.write", lambda fd, data: len(data) - 1)
    with pytest.raises(OSError, match="partly written"):
        append(tmp_path, _entry())


def test_read_missing_directory(tmp_path: Path) -> None:
    assert list(read(tmp_path / "none", NOW)) == []


def test_log_directory(tmp_path: Path) -> None:
    assert log_directory(LogSettings(path=tmp_path)) == tmp_path
    assert log_directory(LogSettings()).name == "log"


def test_entry_reuses_supplied_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(sql: str) -> str:
        pytest.fail("the supplied shape should not be recomputed")

    monkeypatch.setattr("scanisaur.audit.log.shape_fingerprint", unexpected)
    entry = decision(check(SQL, CATALOG), SQL, source="cli", shape_id="s_cached")
    assert entry.shape == "s_cached"
