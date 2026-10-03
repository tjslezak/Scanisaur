"""``scanisaur.yaml``: the project's policy, read into a :class:`Policy`.

Example::

    pricing:
      model: on_demand        # or editions (bytes only)
      usd_per_tib: 6.25
    policy:
      read_only: true
      fail_mode: open         # or closed
      warn_bytes: 100GiB      # or a number of bytes, or off
      block_bytes: 1TiB
      rules:
        SCN005: off           # or info, warn, block
    warehouse:
      type: bigquery          # or duckdb, with path: demo.duckdb
      project: acme-analytics
      location: US
      include_datasets: [analytics, marts]   # optional; every dataset when left out
    cache:
      ttl: 6h
    log:                      # the decision log, one JSON line per check
      enabled: true
      path: ~/scanisaur-log   # a directory; the user state directory when left out
      raw_sql: false          # true also logs each query's SQL, literals included
    keys:                     # unique column sets BigQuery tables don't declare
      acme-analytics.marts.orders: [[order_id]]

``profile`` and ``planner`` are accepted so a whole project file loads, but nothing reads
them yet.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from scanisaur.engine.check import DEFAULT_POLICY, Policy, RuleSetting
from scanisaur.engine.rules import ALL_RULES
from scanisaur.errors import ScanisaurError

#: The file ``scanisaur check`` reads from the working directory when no --config is given.
CONFIG_FILE = "scanisaur.yaml"

_UNITS = {
    "": 1,
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "PB": 10**15,
    "KIB": 2**10,
    "MIB": 2**20,
    "GIB": 2**30,
    "TIB": 2**40,
    "PIB": 2**50,
}
_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd]?)", re.IGNORECASE)
#: A number, with an optional exponent (YAML reads ``1e11`` as a string), and a unit.
_SIZE = re.compile(r"(\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([A-Za-z]*)")


class ConfigError(ScanisaurError, ValueError):
    """A ``scanisaur.yaml`` that can't be read or doesn't match the expected shape."""


def parse_size(value: object) -> int | None:
    """Bytes from ``100GiB``, ``1.5 TB`` or a plain number; None for ``off``.

    YAML reads a bare ``off`` as false, so false means off too.
    """
    if value is None or value is False or value == "off":
        return None
    if isinstance(value, bool):
        raise ValueError("expected a size such as 100GiB, or off")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        if value < 0:
            raise ValueError("a size can't be negative")
        return value
    if isinstance(value, str) and (match := _SIZE.fullmatch(value.strip())):
        number, unit = match.groups()
        factor = _UNITS.get(unit.upper())
        size = float(number) * (factor or 0)
        if factor is not None and math.isfinite(size):
            return round(size)
    raise ValueError(f"expected a size such as 100GiB, 1TB or a number of bytes, or off: {value!r}")


def parse_duration(value: object) -> timedelta:
    """A duration from ``6h``, ``30m``, ``1d``, ``90s`` or a number of seconds."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return timedelta(seconds=value)
    if isinstance(value, str) and (match := _DURATION.fullmatch(value.strip())):
        number, unit = match.groups()
        return timedelta(seconds=float(number) * _SECONDS[unit.lower()])
    raise ValueError(f"expected a duration such as 6h, 30m or a number of seconds: {value!r}")


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _PricingSpec(_Spec):
    model: Literal["on_demand", "editions"] = "on_demand"
    usd_per_tib: float | None = Field(default=None, gt=0)


class _PolicySpec(_Spec):
    read_only: bool = DEFAULT_POLICY.read_only
    fail_mode: Literal["open", "closed"] = DEFAULT_POLICY.fail_mode
    warn_bytes: int | None = DEFAULT_POLICY.warn_bytes
    block_bytes: int | None = DEFAULT_POLICY.block_bytes
    cross_join_warn_pairs: int = Field(default=DEFAULT_POLICY.cross_join_warn_pairs, ge=0)
    cross_join_block_pairs: int = Field(default=DEFAULT_POLICY.cross_join_block_pairs, ge=0)
    unbounded_result_rows: int = Field(default=DEFAULT_POLICY.unbounded_result_rows, ge=0)
    rules: dict[str, RuleSetting] = Field(default_factory=dict)

    @field_validator("warn_bytes", "block_bytes", mode="before")
    @classmethod
    def _size(cls, value: object) -> int | None:
        return parse_size(value)

    @field_validator("rules", mode="before")
    @classmethod
    def _rules(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        unknown = sorted(str(rule) for rule in value if rule not in ALL_RULES)
        if unknown:
            raise ValueError(f"unknown rule {', '.join(unknown)}; rules are {', '.join(ALL_RULES)}")
        # YAML reads a bare `off` as false.
        return {rule: "off" if setting is False else setting for rule, setting in value.items()}


class BigQueryWarehouse(_Spec):
    """A BigQuery project. Credentials come from Application Default Credentials only."""

    type: Literal["bigquery"]
    project: str = Field(min_length=1)
    location: str = "US"
    #: The project billed for metadata queries; the warehouse project when left out.
    billing_project: str | None = None
    #: Datasets to read; every dataset in the project when empty.
    include_datasets: tuple[str, ...] = ()
    exclude_datasets: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """This warehouse's identity, which keys its metadata cache."""
        return f"bigquery:{self.project}:{self.location}"


class DuckDBWarehouse(_Spec):
    """A DuckDB database file, opened read-only. Relative paths are from the config file."""

    type: Literal["duckdb"]
    path: Path
    include_datasets: tuple[str, ...] = ()
    exclude_datasets: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """This warehouse's identity, which keys its metadata cache."""
        return f"duckdb:{self.path.resolve()}"


Warehouse = Annotated[BigQueryWarehouse | DuckDBWarehouse, Field(discriminator="type")]


class CacheSettings(_Spec):
    #: A snapshot older than this is refreshed before it's used.
    ttl: timedelta = timedelta(hours=6)
    #: The SQLite file; one in the user cache directory when left out.
    path: Path | None = None

    @field_validator("ttl", mode="before")
    @classmethod
    def _duration(cls, value: object) -> timedelta:
        return parse_duration(value)


class LogSettings(_Spec):
    """The decision log (docs/decision-log.md)."""

    enabled: bool = True
    #: The directory of monthly files; one in the user state directory when left out.
    path: Path | None = None
    #: Also log each query's SQL. Off by default: SQL can hold values such as emails.
    raw_sql: bool = False


#: Unique column sets per table, such as ``{"p.d.orders": (("order_id",),)}``.
Keys = Mapping[str, tuple[tuple[str, ...], ...]]


class _ConfigSpec(_Spec):
    profile: str | None = None
    warehouse: Warehouse | None = None
    pricing: _PricingSpec = _PricingSpec()
    policy: _PolicySpec = _PolicySpec()
    planner: dict[str, Any] | None = None
    cache: CacheSettings = field(default_factory=CacheSettings)
    log: LogSettings = field(default_factory=LogSettings)
    keys: dict[str, tuple[tuple[str, ...], ...]] = Field(default_factory=dict)

    @field_validator("keys")
    @classmethod
    def _keys(
        cls, keys: dict[str, tuple[tuple[str, ...], ...]]
    ) -> dict[str, tuple[tuple[str, ...], ...]]:
        for table, column_sets in keys.items():
            parts = table.split(".")
            if len(parts) != 3 or not all(parts):
                raise ValueError(f"table name {table!r} must be project.dataset.table")
            if any(not columns for columns in column_sets):
                raise ValueError(f"{table}: a key needs at least one column")
        return keys

    @field_validator("pricing", "policy", "cache", "log", mode="before")
    @classmethod
    def _empty_section(cls, value: object) -> object:
        # A section whose keys are all commented out reads as null.
        return {} if value is None else value


@dataclass(frozen=True, slots=True)
class Config:
    """Everything ``scanisaur.yaml`` sets."""

    policy: Policy = DEFAULT_POLICY
    #: None when the file names no warehouse: checks then need a catalog fixture.
    warehouse: BigQueryWarehouse | DuckDBWarehouse | None = None
    cache: CacheSettings = field(default_factory=CacheSettings)
    log: LogSettings = field(default_factory=LogSettings)
    keys: Keys = field(default_factory=dict)


def load_policy(path: str | os.PathLike[str]) -> Policy:
    """Read the policy from a ``scanisaur.yaml``, raising ConfigError with the reason."""
    return load_config(path).policy


def load_config(path: str | os.PathLike[str]) -> Config:
    """Read a ``scanisaur.yaml``, raising ConfigError with the reason."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error
    return load_config_text(text, path)


def load_config_text(text: str, path: Path) -> Config:
    """Read ``scanisaur.yaml`` text; ``path`` names it in errors and anchors DuckDB paths."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: {error}") from error
    try:
        spec = _ConfigSpec.model_validate({} if data is None else data)
    except ValidationError as error:
        raise ConfigError(f"{path}: {error}") from error
    policy, pricing = spec.policy, spec.pricing
    # Only a warn_bytes the file sets: the default may be over a lower block_bytes, where
    # it does nothing, since the block fires first.
    if (
        "warn_bytes" in policy.model_fields_set
        and policy.warn_bytes is not None
        and policy.block_bytes is not None
        and policy.warn_bytes > policy.block_bytes
    ):
        raise ConfigError(f"{path}: policy.warn_bytes is larger than policy.block_bytes")
    if pricing.model == "editions" and pricing.usd_per_tib is not None:
        raise ConfigError(f"{path}: pricing.usd_per_tib applies to the on_demand model only")
    if pricing.model == "editions":
        price = None
    else:
        price = pricing.usd_per_tib or DEFAULT_POLICY.price_per_tib
    warehouse = spec.warehouse
    if isinstance(warehouse, DuckDBWarehouse) and not warehouse.path.is_absolute():
        warehouse = warehouse.model_copy(update={"path": path.parent / warehouse.path})
    return Config(
        policy=_policy(spec.policy, price),
        warehouse=warehouse,
        cache=spec.cache,
        log=spec.log,
        keys=spec.keys,
    )


def _policy(policy: _PolicySpec, price: float | None) -> Policy:
    return Policy(
        read_only=policy.read_only,
        fail_mode=policy.fail_mode,
        price_per_tib=price,
        cross_join_warn_pairs=policy.cross_join_warn_pairs,
        cross_join_block_pairs=policy.cross_join_block_pairs,
        unbounded_result_rows=policy.unbounded_result_rows,
        warn_bytes=policy.warn_bytes,
        block_bytes=policy.block_bytes,
        rules=policy.rules,
    )
