import pytest

from scanisaur import ScanisaurError
from scanisaur.catalog.fixtures import FixtureError
from scanisaur.engine.facts import FactsError, TooComplexError
from scanisaur.engine.parse import SqlParseError
from scanisaur.engine.resolve import ResolveError


@pytest.mark.parametrize(
    "error", [FixtureError, FactsError, TooComplexError, SqlParseError, ResolveError]
)
def test_every_input_error_is_a_scanisaur_error(error: type[Exception]) -> None:
    assert issubclass(error, ScanisaurError)
