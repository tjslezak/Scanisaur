"""Command-line entry point."""

import dataclasses
import sys
from pathlib import Path
from typing import Annotated

import typer

from scanisaur import __version__
from scanisaur.catalog.fixtures import FixtureError, load_catalog
from scanisaur.catalog.source import FixtureSource
from scanisaur.config import CONFIG_FILE, ConfigError, load_policy
from scanisaur.engine.check import DEFAULT_POLICY, Policy, check
from scanisaur.engine.result import CheckResult, Verdict
from scanisaur.tools import describe_estimate

#: Exit codes for ``scanisaur check``. 2 is also what Click uses for usage errors.
EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2

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
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help=f"Policy file. Default: {CONFIG_FILE} in the working directory, if there is one.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
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

    The policy comes from the policy file; the flags below tighten or change it.

    Exit codes: 0 may run; 1 blocked (or warned, with --strict); 2 usage or input error.
    """
    try:
        loaded = load_catalog(catalog)
        policy = _policy(config)
    except (FixtureError, ConfigError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    try:
        sql = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        typer.echo(f"error: {'standard input' if source == '-' else source}: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error

    if allow_writes:
        policy = dataclasses.replace(policy, read_only=False)
    if fail_closed:
        policy = dataclasses.replace(policy, fail_mode="closed")
    if capacity_pricing:
        policy = dataclasses.replace(policy, price_per_tib=None)
    result = check(sql, loaded, policy=policy)
    typer.echo(result.model_dump_json(indent=2) if as_json else _format(result))

    failing = {Verdict.BLOCK, Verdict.WARN} if strict else {Verdict.BLOCK}
    raise typer.Exit(EXIT_BLOCKED if result.verdict in failing else EXIT_OK)


@app.command("serve")
def serve_command(
    catalog: Annotated[
        Path,
        typer.Option(
            "--catalog",
            "-c",
            help="Catalog fixture (YAML) to answer from.",
            exists=True,
            dir_okay=False,
        ),
    ],
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help=f"Policy file. Default: {CONFIG_FILE} in the working directory, if there is one.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Run the MCP server over standard input and output, for an MCP client to start."""
    try:
        source = FixtureSource(catalog)
        policy = _policy(config)
    except (FixtureError, ConfigError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    # Imported here: the MCP SDK takes about a second to import, and only serve needs it.
    from scanisaur.server import build_server

    build_server(source, policy).run("stdio")


def _policy(config: Path | None) -> Policy:
    if config is None and Path(CONFIG_FILE).is_file():
        config = Path(CONFIG_FILE)
    return DEFAULT_POLICY if config is None else load_policy(config)


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
        lines.append(f"estimate: {describe_estimate(result.estimate)}")
    lines.append(f"tag: {result.tag}")
    return "\n".join(lines)
