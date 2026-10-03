"""Command-line entry point."""

import dataclasses
import sys
import time
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, assert_never

import typer
import yaml

from scanisaur import __version__
from scanisaur.catalog.cached import CachedSource
from scanisaur.catalog.connectors import Probe
from scanisaur.catalog.fixtures import load_catalog
from scanisaur.catalog.source import FixtureSource
from scanisaur.config import CONFIG_FILE, Config, ConfigError, load_config, load_config_text
from scanisaur.engine.check import check
from scanisaur.engine.result import CheckResult, Verdict
from scanisaur.errors import ScanisaurError
from scanisaur.hook import main as hook_main
from scanisaur.hook import policy_file, socket_path
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
        Path | None,
        typer.Option(
            "--catalog",
            "-c",
            help="Catalog fixture (YAML) to check against. Default: the warehouse in the "
            "policy file, through the local cache.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
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
        settings = _config(config)
        if catalog is None and settings.warehouse is None:
            raise ConfigError(f"give --catalog, or name a warehouse in {CONFIG_FILE}")
        fixture = None if catalog is None else load_catalog(catalog)
    except ScanisaurError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    policy = settings.policy
    try:
        sql = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        typer.echo(f"error: {'standard input' if source == '-' else source}: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    try:
        loaded = fixture or CachedSource(settings).snapshot_for(sql).catalog
    except ScanisaurError as error:
        typer.echo(f"error: {error}", err=True)
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
        policy = _config(config).policy
    except ScanisaurError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    # Imported here: the MCP SDK takes about a second to import, and only serve needs it.
    from scanisaur.server import build_server

    hook_socket = socket_path(catalog, policy_file(config))
    build_server(source, policy, hook_socket).run("stdio")


@app.command(
    "hook",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def hook_command(ctx: typer.Context) -> None:
    """Check the SQL in an agent's tool call, for an agent harness to run before the
    call. Talks to a running `serve` started with the same --catalog and --config."""
    # `scanisaur hook` normally starts in scanisaur.__main__, skipping this module's imports.
    raise typer.Exit(hook_main(ctx.args))


@app.command("refresh")
def refresh_command(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help=f"Policy file. Default: {CONFIG_FILE} in the working directory.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Fetch the warehouse's metadata into the local cache now."""
    start = time.perf_counter()
    try:
        snapshot = CachedSource(_config(config)).refresh()
    except ScanisaurError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    count = len(snapshot.catalog.tables)
    typer.echo(
        f"refreshed {count} table{'' if count == 1 else 's'} in {time.perf_counter() - start:.1f} s"
    )


#: Catalog-only access for BigQuery (docs/bigquery-setup.md, spike 0003).
_GCLOUD = """\
SA=scanisaur-catalog@{project}.iam.gserviceaccount.com
gcloud iam service-accounts create scanisaur-catalog --project={project} \\
  --display-name="Scanisaur catalog-only"
for role in bigquery.metadataViewer bigquery.jobUser; do
  gcloud projects add-iam-policy-binding {project} --member="serviceAccount:$SA" \\
    --role="roles/$role" --condition=None
done
gcloud auth application-default login --impersonate-service-account="$SA"
"""


class WarehouseKind(StrEnum):
    BIGQUERY = "bigquery"
    DUCKDB = "duckdb"


@app.command("init")
def init_command(
    warehouse: Annotated[
        WarehouseKind | None,
        typer.Option(help="bigquery or duckdb. Asked for when left out."),
    ] = None,
    project: Annotated[str | None, typer.Option(help="BigQuery project.")] = None,
    location: Annotated[str | None, typer.Option(help="BigQuery location, such as US.")] = None,
    dataset: Annotated[
        list[str] | None,
        typer.Option(help="Dataset to read; repeat for several. Default: every dataset."),
    ] = None,
    path: Annotated[str | None, typer.Option(help="DuckDB database file.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a scanisaur.yaml for a warehouse, and print the access it needs."""
    target = Path(CONFIG_FILE)
    if target.exists() and not force:
        typer.echo(f"error: {target} exists; add --force to overwrite it", err=True)
        raise typer.Exit(EXIT_ERROR)
    kind = warehouse or _warehouse_kind(
        typer.prompt("Warehouse (bigquery or duckdb)", default="bigquery")
    )
    match kind:
        case WarehouseKind.DUCKDB:
            path = path or typer.prompt("DuckDB database file")
            section = f"  type: duckdb\n  path: {_quoted(path)}\n"
        case WarehouseKind.BIGQUERY:
            project = project or typer.prompt("BigQuery project")
            location = location or typer.prompt("Location", default="US")
            if dataset is None and warehouse is None:
                answer = typer.prompt("Datasets, comma-separated (blank for all)", default="")
                dataset = [d.strip() for d in answer.split(",") if d.strip()]
            section = (
                f"  type: bigquery\n  project: {_quoted(project)}\n"
                f"  location: {_quoted(location)}\n"
            )
            if dataset:
                section += f"  include_datasets: [{', '.join(_quoted(d) for d in dataset)}]\n"
        case _:
            assert_never(kind)
    text = (
        f"warehouse:\n{section}"
        "cache:\n  ttl: 6h\n"
        "policy:\n  warn_bytes: 100GiB\n  block_bytes: 1TiB\n"
        "# keys:                 # column sets unique in a table, for SCN007\n"
        "#   project.dataset.table: [[id]]\n"
    )
    try:
        load_config_text(text, target)
    except ConfigError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    target.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {target}")
    if kind is WarehouseKind.BIGQUERY:
        typer.echo("\nGive Scanisaur catalog-only access (metadata, never table data):\n")
        typer.echo(_GCLOUD.format(project=project))
    typer.echo("Then run `scanisaur doctor` to confirm access.")


def _warehouse_kind(answer: str) -> WarehouseKind:
    try:
        return WarehouseKind(answer.strip().lower())
    except ValueError as error:
        raise typer.BadParameter(
            f"{answer!r}; use bigquery or duckdb", param_hint="warehouse"
        ) from error


def _quoted(value: str) -> str:
    """A YAML scalar for ``value``, quoted only when it needs to be."""
    return yaml.safe_dump(value, default_style=None).removesuffix("\n...\n").strip()


@app.command("doctor")
def doctor_command(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help=f"Policy file. Default: {CONFIG_FILE} in the working directory.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Check access to the warehouse: metadata readable, table data not.

    Exit codes: 0 every check passed; 1 a warning or failure; 2 no usable policy file.
    """
    try:
        settings = _config(config)
        source = CachedSource(settings)
        connector = source.connector
    except ScanisaurError as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(EXIT_ERROR) from error
    probes = [*connector.check_access(), _cache_probe(source, settings)]
    for probe in probes:
        typer.echo(f"{probe.status:<5} {probe.name}: {probe.detail}")
    raise typer.Exit(EXIT_OK if all(p.status == "ok" for p in probes) else EXIT_BLOCKED)


def _cache_probe(source: CachedSource, config: Config) -> Probe:
    try:
        snapshot = source.cached()
    except ScanisaurError as error:
        return Probe("cache", "fail", str(error))
    if snapshot is None or snapshot.fetched_at is None:
        return Probe("cache", "ok", "empty: the first check or `scanisaur refresh` fills it")
    age = datetime.now(UTC) - snapshot.fetched_at
    count = len(snapshot.catalog.tables)
    detail = f"{count} tables, refreshed {_age(age)} ago"
    if age > config.cache.ttl:
        return Probe("cache", "ok", f"{detail}; the next check refreshes it")
    return Probe("cache", "ok", detail)


def _age(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _config(path: Path | None) -> Config:
    path = policy_file(path)
    return Config() if path is None else load_config(path)


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

