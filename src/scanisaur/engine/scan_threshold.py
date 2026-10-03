"""SCN010: the estimated bytes billed reach the policy's warn or block threshold."""

from __future__ import annotations

from scanisaur.engine.pruning import format_bytes
from scanisaur.engine.result import Estimate, Finding, Severity
from scanisaur.engine.rules import SCAN_THRESHOLD

_UNITS = (
    *(("PiB", 2**50), ("TiB", 2**40), ("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)),
    *(("PB", 10**15), ("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)),
)
_FIX = (
    "Read less: filter on the partition column, or select only the columns you need. "
    "If the scan is intended, a person can raise the threshold in scanisaur.yaml."
)


def scan_threshold_findings(
    estimate: Estimate, *, warn_bytes: int | None, block_bytes: int | None
) -> list[Finding]:
    """Compares the low end of the estimate, what the query bills at least, with each
    threshold. The high end assumes clustering and unevaluated filters skip nothing, so it
    is far above most bills; it only warns when it reaches ``block_bytes``, and not at low
    confidence, where it assumes no partition is skipped either."""
    low, high = estimate.bytes_low, estimate.bytes_high
    if block_bytes is not None and low >= block_bytes:
        message = (
            f"The query would bill {_span(low, high)}, at or over the block threshold of "
            f"{format_threshold(block_bytes)}."
        )
        return [_finding(Severity.BLOCK, message)]
    if warn_bytes is not None and low >= warn_bytes:
        message = (
            f"The query would bill {_span(low, high)}, at or over the warn threshold of "
            f"{format_threshold(warn_bytes)}."
        )
        return [_finding(Severity.WARN, message)]
    if block_bytes is not None and high >= block_bytes and estimate.confidence != "low":
        message = (
            f"The query could bill up to {format_bytes(high)}, at or over the block threshold "
            f"of {format_threshold(block_bytes)}; it warns because the low end of the "
            f"estimate, {format_bytes(low)}, is under it."
        )
        return [_finding(Severity.WARN, message)]
    return []


def format_threshold(size: int) -> str:
    """A threshold in the unit it was most likely written in, such as ``100 GiB`` or
    ``300 GB``: the one that divides it with the smallest whole number."""
    exact = [(size // factor, unit) for unit, factor in _UNITS if size and size % factor == 0]
    if not exact:
        return format_bytes(size)
    number, unit = min(exact)
    return f"{number} {unit}"


def _span(low: int, high: int) -> str:
    shown = format_bytes(low), format_bytes(high)
    return shown[0] if shown[0] == shown[1] else f"{shown[0]}-{shown[1]}"


def _finding(severity: Severity, message: str) -> Finding:
    return Finding(rule=SCAN_THRESHOLD, severity=severity, message=message, fix=_FIX)
