"""``CachedSource``: a warehouse's catalog, read through the local SQLite cache."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import platformdirs

from scanisaur.catalog.cache import MetadataCache
from scanisaur.catalog.connectors import Connector, ConnectorError, connect
from scanisaur.catalog.connectors.base import with_keys
from scanisaur.catalog.model import Catalog
from scanisaur.catalog.source import SearchHit, Snapshot
from scanisaur.config import Config, ConfigError
from scanisaur.engine.parse import DIALECT, SqlParseError, parse, resolvable
from scanisaur.engine.resolve import unknown_tables

logger = logging.getLogger(__name__)


class CachedSource:
    """A :class:`~scanisaur.catalog.source.CatalogSource` over a warehouse.

    The snapshot is kept in memory. ``current()`` refreshes it from the warehouse when
    there is none yet or it is older than the configured TTL; if that refresh fails, it
    keeps the stale snapshot and logs a warning.
    """

    def __init__(
        self,
        config: Config,
        *,
        connector: Connector | None = None,
        cache: MetadataCache | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if config.warehouse is None and connector is None:
            raise ConfigError("scanisaur.yaml names no warehouse")
        self._config = config
        self._connector = connector
        self._cache = cache or MetadataCache(_cache_path(config, self.connector.name))
        self._now = now
        self._snapshot: Snapshot | None = None

    @property
    def connector(self) -> Connector:
        if self._connector is None:
            assert self._config.warehouse is not None
            self._connector = connect(self._config.warehouse)
        return self._connector

    def cached(self) -> Snapshot | None:
        """The saved snapshot, without contacting the warehouse."""
        if self._snapshot is None:
            self._snapshot = self._cache.load()
        return self._snapshot

    def current(self) -> Snapshot:
        snapshot = self.cached()
        if snapshot is None:
            return self.refresh()
        if self._stale(snapshot):
            try:
                return self.refresh()
            except ConnectorError as error:
                logger.warning("using a stale catalog, refresh failed: %s", error)
        return snapshot

    def snapshot_for(self, sql: str) -> Snapshot:
        """The current snapshot, with any table ``sql`` reads that it lacks fetched once.

        A table created since the last refresh would otherwise be reported as unknown
        (SCN001) until the next one.
        """
        snapshot = self.current()
        try:
            statements = parse(sql, DIALECT)
        except SqlParseError:
            return snapshot  # check() reports it
        missing = {
            name
            for statement in statements
            if (tree := resolvable(statement)) is not None
            for name in unknown_tables(tree, snapshot.catalog)
        }
        for project, dataset, name in sorted(missing):
            try:
                table = self.connector.fetch_table(project, dataset, name)
            except ConnectorError as error:
                logger.warning("couldn't look up %s.%s.%s: %s", project, dataset, name, error)
                continue
            if table is not None:
                table = with_keys(table, self._config.keys.get(table.qualified_name))
                snapshot = self._cache.put_table(snapshot, table)
        self._snapshot = snapshot
        return snapshot

    def refresh(self) -> Snapshot:
        """Fetch the whole catalog now and make it current."""
        catalog = self.connector.fetch_catalog()
        self._snapshot = self._cache.save(self._with_keys(catalog), warehouse=self.connector.name)
        return self._snapshot

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.current()
        return self._cache.search(query, limit)

    def _stale(self, snapshot: Snapshot) -> bool:
        fetched = snapshot.fetched_at
        return fetched is None or self._now() - fetched > self._config.cache.ttl

    def _with_keys(self, catalog: Catalog) -> Catalog:
        keys = self._config.keys
        if not keys:
            return catalog
        tables = tuple(with_keys(t, keys.get(t.qualified_name)) for t in catalog.tables)
        return replace(catalog, tables=tables)


def _cache_path(config: Config, warehouse: str) -> Path:
    if config.cache.path is not None:
        return config.cache.path.expanduser()
    digest = hashlib.sha256(warehouse.encode()).hexdigest()[:16]
    return Path(platformdirs.user_cache_dir("scanisaur")) / f"{digest}.sqlite"
