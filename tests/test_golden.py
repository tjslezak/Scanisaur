"""Golden tests: each ``tests/golden/<rule>/<case>.sql`` has an expected ``.json`` result.

The first line of a case may set the policy, for example::

    -- policy: {"read_only": false}

After an intended change, regenerate the expected files with
``uv run pytest tests/test_golden.py --update-golden`` and review the diff.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from scanisaur.catalog.fixtures import load_catalog
from scanisaur.engine.check import Policy, check

GOLDEN = Path(__file__).parent / "golden"
CATALOG = load_catalog(GOLDEN / "catalog.yaml")
CASES = sorted(GOLDEN.glob("*/*.sql"))
POLICY_PREFIX = "-- policy:"


def _policy(sql: str) -> Policy:
    first_line = sql.partition("\n")[0]
    if not first_line.startswith(POLICY_PREFIX):
        return Policy()
    return Policy(**json.loads(first_line.removeprefix(POLICY_PREFIX)))


def _result(case: Path) -> dict[str, Any]:
    sql = case.read_text(encoding="utf-8")
    result = check(sql, CATALOG, policy=_policy(sql))
    # The ID and tag differ on every run; everything else must be deterministic.
    return result.model_dump(mode="json", exclude={"check_id", "tag"})


def test_cases_exist() -> None:
    assert len(CASES) >= 40


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.parent.name}/{case.stem}")
def test_golden(case: Path, update_golden: bool) -> None:
    expected_path = case.with_suffix(".json")
    actual = _result(case)
    if update_golden:
        expected_path.write_text(json.dumps(actual, indent=2) + "\n", encoding="utf-8")
        return
    assert expected_path.exists(), f"no expected output; run pytest with --update-golden: {case}"
    assert actual == json.loads(expected_path.read_text(encoding="utf-8"))
