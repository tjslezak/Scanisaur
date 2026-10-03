from datetime import timedelta
from pathlib import Path

import pytest

from scanisaur.config import (
    BigQueryWarehouse,
    CacheSettings,
    Config,
    ConfigError,
    DuckDBWarehouse,
    load_config,
    load_policy,
    parse_duration,
    parse_size,
)
from scanisaur.engine.check import DEFAULT_POLICY, Policy
from scanisaur.errors import ScanisaurError

#: The example in the product requirements' build plan, whole.
PLAN_EXAMPLE = """\
profile: default
warehouse:
  type: bigquery
  project: acme-analytics
  location: US
  include_datasets: [analytics, marts]
pricing:
  model: on_demand        # or editions (bytes only)
  usd_per_tib: 6.25       # verify current list price
policy:
  read_only: true
  fail_mode: open         # or closed
  warn_bytes: 100GiB
  block_bytes: 1TiB
  rules:
    SCN005: off
planner:
  enabled: false
  profile_tables_over: 10GiB
cache:
  ttl: 6h
"""


def _load(tmp_path: Path, text: str) -> Policy:
    path = tmp_path / "scanisaur.yaml"
    path.write_text(text, encoding="utf-8")
    return load_policy(path)


def test_plan_example(tmp_path: Path) -> None:
    policy = _load(tmp_path, PLAN_EXAMPLE)
    assert policy == Policy(rules={"SCN005": "off"})


def test_empty_file_is_the_default(tmp_path: Path) -> None:
    assert _load(tmp_path, "") == DEFAULT_POLICY


def test_every_policy_field(tmp_path: Path) -> None:
    text = """\
pricing: {model: editions}
policy:
  read_only: false
  fail_mode: closed
  warn_bytes: 500 GB
  block_bytes: 2000000000000
  cross_join_warn_pairs: 1000
  cross_join_block_pairs: 100000
  rules: {SCN010: block, SCN011: info}
"""
    assert _load(tmp_path, text) == Policy(
        read_only=False,
        fail_mode="closed",
        price_per_tib=None,
        warn_bytes=500 * 10**9,
        block_bytes=2 * 10**12,
        cross_join_warn_pairs=1000,
        cross_join_block_pairs=100000,
        rules={"SCN010": "block", "SCN011": "info"},
    )


def test_custom_price(tmp_path: Path) -> None:
    assert _load(tmp_path, "pricing: {usd_per_tib: 5}").price_per_tib == 5


def test_thresholds_off(tmp_path: Path) -> None:
    policy = _load(tmp_path, "policy: {warn_bytes: off, block_bytes: null}")
    assert (policy.warn_bytes, policy.block_bytes) == (None, None)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("policy: {warn_bytes: 2TiB}", "warn_bytes is larger than policy.block_bytes"),
        ("policy: {warn_bytes: 100 parsecs}", "expected a size"),
        ("policy: {warn_bytes: true}", "expected a size"),
        ("policy: {warn_bytes: -1}", "can't be negative"),
        ("policy: {rules: {SCN999: off}}", "unknown rule SCN999"),
        ("policy: {rules: {SCN005: loud}}", "SCN005"),
        ("policy: {fail_mode: maybe}", "fail_mode"),
        ("policy: {readonly: true}", "readonly"),
        ("pricing: {model: editions, usd_per_tib: 6.25}", "on_demand model only"),
        ("pricing: {usd_per_tib: 0}", "usd_per_tib"),
        ("polcy: {}", "polcy"),
        ("- a list", "valid dictionary"),
        ("policy: [unclosed", "while parsing"),
    ],
)
def test_bad_file(tmp_path: Path, text: str, reason: str) -> None:
    with pytest.raises(ConfigError, match=r"scanisaur\.yaml") as error:
        _load(tmp_path, text)
    assert reason in str(error.value)
    assert isinstance(error.value, ScanisaurError)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No such file"):
        load_policy(tmp_path / "scanisaur.yaml")


@pytest.mark.parametrize(
    ("value", "size"),
    [
        (0, 0),
        (1024, 1024),
        (1e11, 10**11),
        ("100GiB", 100 * 2**30),
        ("1 TiB", 2**40),
        ("1.5TB", 15 * 10**11),
        ("10 mb", 10**7),
        ("42", 42),
        ("1e11", 10**11),
        ("2.5e3 KB", 2_500_000),
        ("1E+3", 1000),
        ("7 B", 7),
        ("off", None),
        (False, None),
        (None, None),
    ],
)
def test_parse_size(value: object, size: int | None) -> None:
    assert parse_size(value) == size


@pytest.mark.parametrize("value", ["", "GiB", "1 XB", "1e", "e3", "1e400", 1.5, [1]])
def test_parse_size_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="expected a size"):
        parse_size(value)


def test_block_bytes_alone_under_the_default_warn(tmp_path: Path) -> None:
    policy = _load(tmp_path, "policy: {block_bytes: 50GB}")
    assert (policy.warn_bytes, policy.block_bytes) == (100 * 2**30, 50 * 10**9)


@pytest.mark.parametrize("text", ["policy:\n  # read_only: true\n", "pricing:\n"])
def test_empty_section_is_the_default(tmp_path: Path, text: str) -> None:
    assert _load(tmp_path, text) == DEFAULT_POLICY


def test_exponent_from_yaml(tmp_path: Path) -> None:
    assert _load(tmp_path, "policy: {warn_bytes: 1e11}").warn_bytes == 10**11


def _config(tmp_path: Path, text: str) -> Config:
    path = tmp_path / "scanisaur.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_plan_example_warehouse(tmp_path: Path) -> None:
    config = _config(tmp_path, PLAN_EXAMPLE)
    assert config.warehouse == BigQueryWarehouse(
        type="bigquery",
        project="acme-analytics",
        location="US",
        include_datasets=("analytics", "marts"),
    )
    assert config.cache == CacheSettings(ttl=timedelta(hours=6))


def test_no_warehouse(tmp_path: Path) -> None:
    assert _config(tmp_path, "") == Config()


def test_duckdb_path_is_relative_to_the_file(tmp_path: Path) -> None:
    config = _config(tmp_path, "warehouse: {type: duckdb, path: demo.duckdb}")
    assert config.warehouse == DuckDBWarehouse(type="duckdb", path=tmp_path / "demo.duckdb")


def test_cache_settings(tmp_path: Path) -> None:
    config = _config(tmp_path, "cache: {ttl: 30m, path: /tmp/c.sqlite}")
    assert config.cache == CacheSettings(ttl=timedelta(minutes=30), path=Path("/tmp/c.sqlite"))


def test_keys(tmp_path: Path) -> None:
    config = _config(tmp_path, "keys: {p.d.orders: [[order_id], [shop, number]]}")
    assert config.keys == {"p.d.orders": (("order_id",), ("shop", "number"))}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("warehouse: {type: snowflake}", "snowflake"),
        ("warehouse: {type: bigquery}", "project"),
        ("warehouse: {type: duckdb, path: x.duckdb, project: p}", "project"),
        ("warehouse: {type: bigquery, project: p, datasets: [a]}", "datasets"),
        ("cache: {ttl: soon}", "expected a duration"),
        ("keys: {orders: [[id]]}", "must be project.dataset.table"),
        ("keys: {p.d.orders: [[]]}", "at least one column"),
    ],
)
def test_bad_warehouse_cache_or_keys(tmp_path: Path, text: str, reason: str) -> None:
    with pytest.raises(ConfigError, match=r"scanisaur\.yaml") as error:
        _config(tmp_path, text)
    assert reason in str(error.value)


@pytest.mark.parametrize(
    ("value", "duration"),
    [
        ("6h", timedelta(hours=6)),
        ("30m", timedelta(minutes=30)),
        ("1d", timedelta(days=1)),
        ("90 s", timedelta(seconds=90)),
        ("1.5H", timedelta(minutes=90)),
        ("45", timedelta(seconds=45)),
        (3600, timedelta(hours=1)),
    ],
)
def test_parse_duration(value: object, duration: timedelta) -> None:
    assert parse_duration(value) == duration


@pytest.mark.parametrize("value", ["", "h", "6 weeks", -1, True, [1]])
def test_parse_duration_rejects(value: object) -> None:
    with pytest.raises(ValueError, match="expected a duration"):
        parse_duration(value)
