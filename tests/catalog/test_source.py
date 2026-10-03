from pathlib import Path

import pytest

from scanisaur.catalog.fixtures import FixtureError
from scanisaur.catalog.model import Catalog, Column, Table
from scanisaur.catalog.source import CatalogSource, FixtureSource, SearchHit, search_catalog

GOLDEN_CATALOG = Path(__file__).parents[1] / "golden" / "catalog.yaml"


def _table(name: str, *columns: str, description: str = "") -> Table:
    return Table(
        "proj",
        "shop",
        name,
        columns=tuple(Column(c, "STRING") for c in columns),
        description=description,
    )


class TestFixtureSource:
    def test_is_a_catalog_source(self) -> None:
        source: CatalogSource = FixtureSource(GOLDEN_CATALOG)
        assert source.current().catalog.find("events") is not None

    def test_snapshot_is_the_same_object_every_time(self) -> None:
        source = FixtureSource(GOLDEN_CATALOG)
        assert source.current() is source.current()

    def test_snapshot_id_follows_the_file_content(self, tmp_path: Path) -> None:
        path = tmp_path / "catalog.yaml"
        path.write_bytes(GOLDEN_CATALOG.read_bytes())
        first = FixtureSource(path).current().snapshot_id
        assert first.startswith("fixture_")
        assert FixtureSource(path).current().snapshot_id == first
        path.write_bytes(GOLDEN_CATALOG.read_bytes() + b"\n# changed\n")
        assert FixtureSource(path).current().snapshot_id != first

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FixtureError, match=r"missing\.yaml"):
            FixtureSource(tmp_path / "missing.yaml")

    def test_search(self) -> None:
        hits = FixtureSource(GOLDEN_CATALOG).search("order", 3)
        assert len(hits) == 3
        assert hits[0] == SearchHit("proj.analytics.Orders", None, 3.0)


class TestSearchCatalog:
    catalog = Catalog(
        (
            _table("orders", "order_id", "user_id"),
            _table("users", "user_id", "country", description="One row per customer"),
        )
    )

    def test_table_name_outranks_column_name(self) -> None:
        hits = search_catalog(self.catalog, "user", 10)
        assert hits[:3] == [
            SearchHit("proj.shop.users", None, 3.0),
            SearchHit("proj.shop.orders", "user_id", 2.0),
            SearchHit("proj.shop.users", "user_id", 2.0),
        ]

    def test_each_word_adds_points(self) -> None:
        hits = search_catalog(self.catalog, "user country", 1)
        assert hits == [SearchHit("proj.shop.users", None, 3.0)]
        assert SearchHit("proj.shop.users", "country", 2.0) in search_catalog(
            self.catalog, "user country", 10
        )

    def test_description_matches(self) -> None:
        assert search_catalog(self.catalog, "Customer", 10) == [
            SearchHit("proj.shop.users", None, 1.0)
        ]

    @pytest.mark.parametrize(("query", "limit"), [("", 10), ("!!", 10), ("user", 0)])
    def test_nothing_to_find(self, query: str, limit: int) -> None:
        assert search_catalog(self.catalog, query, limit) == []
