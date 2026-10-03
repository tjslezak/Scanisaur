"""Warehouse connectors. Drivers are optional extras, imported only when used."""

from __future__ import annotations

from typing import assert_never

from scanisaur.catalog.connectors.base import Connector, ConnectorError, Probe
from scanisaur.config import BigQueryWarehouse, DuckDBWarehouse

__all__ = ["Connector", "ConnectorError", "Probe", "connect"]


def connect(warehouse: BigQueryWarehouse | DuckDBWarehouse) -> Connector:
    """The connector for ``warehouse``, raising ConnectorError when its driver is missing."""
    match warehouse:
        case DuckDBWarehouse():
            try:
                from scanisaur.catalog.connectors.duckdb import DuckDBConnector
            except ImportError as error:
                raise ConnectorError(_install("duckdb")) from error
            return DuckDBConnector(warehouse)
        case BigQueryWarehouse():
            raise ConnectorError("the BigQuery connector isn't built yet")
        case _:
            assert_never(warehouse)


def _install(extra: str) -> str:
    return f"the {extra} driver isn't installed: install scanisaur[{extra}]"
