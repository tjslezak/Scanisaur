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

``profile``, ``warehouse``, ``planner`` and ``cache`` are accepted so a whole project file
loads, but nothing reads them yet.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Literal

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


class _ConfigSpec(_Spec):
    profile: str | None = None
    warehouse: dict[str, Any] | None = None
    pricing: _PricingSpec = _PricingSpec()
    policy: _PolicySpec = _PolicySpec()
    planner: dict[str, Any] | None = None
    cache: dict[str, Any] | None = None

    @field_validator("pricing", "policy", mode="before")
    @classmethod
    def _empty_section(cls, value: object) -> object:
        # A section whose keys are all commented out reads as null.
        return {} if value is None else value


def load_policy(path: str | os.PathLike[str]) -> Policy:
    """Read the policy from a ``scanisaur.yaml``, raising ConfigError with the reason."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
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
