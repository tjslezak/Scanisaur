import runpy

import pytest
from typer.testing import CliRunner

from scanisaur import __version__
from scanisaur.cli import app

runner = CliRunner()


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"scanisaur {__version__}"


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"scanisaur {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_python_dash_m(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", ["scanisaur", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("scanisaur", run_name="__main__")
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"scanisaur {__version__}"
