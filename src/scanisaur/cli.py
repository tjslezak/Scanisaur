"""Command-line entry point."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

import typer

from scanisaur import __version__
from scanisaur.catalog.fixtures import FixtureError, load_catalog
from scanisaur.engine.check import Policy, check
from scanisaur.engine.pruning import format_bytes
from scanisaur.engine.result import CheckResult, Estimate, Verdict

#: Exit codes for ``scanisaur check``. 2 is also what Click uses for usage errors.
EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2

_N = TypeVar("_N", int, float)

app = typer.Typer(
    name="scanisaur",
    help="Schema-aware pre-flight checks for AI-agent SQL.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"scanisaur {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Schema-aware pre-flight checks for AI-agent SQL."""


@app.command("version")
def version_command() -> None:
    """Show the version."""
    typer.echo(f"scanisaur {__version__}")


@app.command("check")
def check_command(
    catalog: Annotated[
        Path,
        typer.Option(
            "--catalog",
            "-c",
            help="Catalog fixture (YAML) to check against.",
            exists=True,
            dir_okay=False,
        ),
    ],
    source: Annotated[
        str, typer.Argument(help="SQL file to check, or '-' to read standard input.")
    ] = "-",
    allow_writes: Annotated[
        bool, typer.Option("--allow-writes", help="Don't block write and DDL statements.")
    ] = False,
    fail_closed: Annotated[
        bool,
        typer.Option("--fail-closed", help="Block, instead of warn, when SQL can't be analyzed."),
    ] = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Exit with 1 on warnings as well as blocks.")
    ] = False,
    capacity_pricing: Annotated[
        bool,
        typer.Option(
            "--capacity-pricing", help="Capacity (Editions) pricing: estimate bytes, not dollars."
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full result as JSON.")] = False,
) -> None:
    """Check one BigQuery SQL statement.

    Exit codes: 0 may run; 1 blocked (or warned, with --strict); 2 usage or input error.
    """
    try:
        loaded = load_catalog(catalog)
    except FixtureError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    try:
        sql = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        typer.echo(f"error: {'standard input' if source == '-' else source}: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error

    policy = Policy(
        read_only=not allow_writes,
        fail_mode="closed" if fail_closed else "open",
        price_per_tib=None if capacity_pricing else Policy().price_per_tib,
    )
    result = check(sql, loaded, policy=policy)
    typer.echo(result.model_dump_json(indent=2) if as_json else _format(result))

    failing = {Verdict.BLOCK, Verdict.WARN} if strict else {Verdict.BLOCK}
    raise typer.Exit(EXIT_BLOCKED if result.verdict in failing else EXIT_OK)


def _format(result: CheckResult) -> str:
    count = len(result.findings)
    header = f"{result.verdict.value}: {count} finding{'' if count == 1 else 's'}"
    if result.tables:
        header += f" · reads {', '.join(result.tables)}"
    lines = [header]
    for finding in result.findings:
        where = f"{finding.line}:{finding.column}" if finding.line is not None else "-"
        lines.append(f"  {where:<7} {finding.rule}  {finding.severity.value:<5}  {finding.message}")
        if finding.fix:
            lines.append(f"  {'':<7} {'':<6}  fix:   {finding.fix}")
    if result.estimate is not None:
        lines.append(f"estimate: {_estimate(result.estimate)}")
    lines.append(f"tag: {result.tag}")
    return "\n".join(lines)


def _estimate(estimate: Estimate) -> str:
    """For example ``1.6-2.4 GB billed, $0.01-$0.02 (medium confidence)``."""
    shown = _span(estimate.bytes_low, estimate.bytes_high, format_bytes) + " billed"
    if estimate.usd_low is not None and estimate.usd_high is not None:
        shown += ", " + _span(estimate.usd_low, estimate.usd_high, _dollars)
    return f"{shown} ({estimate.confidence} confidence)"


def _span(low: _N, high: _N, show: Callable[[_N], str]) -> str:
    shown = show(low), show(high)
    return shown[0] if shown[0] == shown[1] else f"{shown[0]}-{shown[1]}"


def _dollars(value: float) -> str:
    return "<$0.01" if 0 < value < 0.005 else f"${value:.2f}"
