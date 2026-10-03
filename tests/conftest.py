import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        help="Rewrite the expected .json files of golden cases from the current output.",
    )


@pytest.fixture
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))


@pytest.fixture(autouse=True)
def _decision_log(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep tests' checks out of the user's decision log."""
    directory = tmp_path_factory.mktemp("log")
    monkeypatch.setattr("scanisaur.audit.log.platformdirs.user_state_dir", lambda _: str(directory))
