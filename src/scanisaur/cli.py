"""Command-line entry point."""

from typing import Annotated

import typer

from scanisaur import __version__

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
