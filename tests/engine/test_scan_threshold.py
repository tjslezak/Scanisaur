import pytest

from scanisaur.engine.result import Confidence, Estimate, Severity
from scanisaur.engine.scan_threshold import format_threshold, scan_threshold_findings

GIB = 2**30
TIB = 2**40


def _findings(
    low: int, high: int, confidence: Confidence = "high", warn: int | None = 100 * GIB
) -> list[tuple[Severity, str]]:
    estimate = Estimate(bytes_low=low, bytes_high=high, confidence=confidence)
    found = scan_threshold_findings(estimate, warn_bytes=warn, block_bytes=TIB)
    return [(finding.severity, finding.message) for finding in found]


def test_under_both_thresholds() -> None:
    assert _findings(GIB, 2 * GIB) == []


def test_high_end_alone_does_not_warn() -> None:
    assert _findings(GIB, 500 * GIB) == []


def test_low_end_reaches_warn() -> None:
    assert _findings(100 * GIB, 200 * GIB) == [
        (
            Severity.WARN,
            "The query would bill 107.4 GB-214.7 GB, at or over the warn threshold of 100 GiB.",
        )
    ]


def test_low_end_reaches_block() -> None:
    assert _findings(TIB, 2 * TIB) == [
        (
            Severity.BLOCK,
            "The query would bill 1.1 TB-2.2 TB, at or over the block threshold of 1 TiB.",
        )
    ]


def test_high_end_reaching_block_warns() -> None:
    assert _findings(GIB, 2 * TIB, confidence="medium") == [
        (
            Severity.WARN,
            "The query could bill up to 2.2 TB, at or over the block threshold of 1 TiB; it "
            "warns because the low end of the estimate, 1.1 GB, is under it.",
        )
    ]


def test_low_confidence_high_end_is_ignored() -> None:
    assert _findings(10 * 2**20, 3 * TIB, confidence="low") == []
    assert _findings(200 * GIB, 3 * TIB, confidence="low")[0][0] is Severity.WARN
    assert _findings(TIB, 3 * TIB, confidence="low")[0][0] is Severity.BLOCK


def test_warn_off() -> None:
    assert _findings(500 * GIB, 500 * GIB, warn=None) == []
    assert _findings(GIB, 2 * TIB, warn=None)[0][0] is Severity.WARN


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        (100 * GIB, "100 GiB"),
        (TIB, "1 TiB"),
        (300 * 10**9, "300 GB"),
        (1536 * GIB, "1536 GiB"),
        (10**6, "1 MB"),
        (1, "1 B"),
        (0, "0 B"),
        (1001, "1 KB"),
    ],
)
def test_format_threshold(size: int, shown: str) -> None:
    assert format_threshold(size) == shown
