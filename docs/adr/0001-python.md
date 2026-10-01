# 0001: Build Core in Python

- **Status:** Accepted
- **Date:** 2026-10-01

## Context

Scanisaur's core job is to parse SQL in several warehouse dialects and resolve every table and column against a catalog. It ships as an MCP server that should install with one command. Contributors are most likely to be data engineers.

## Decision

Build Core in Python (3.11+), using:

- [`sqlglot`](https://github.com/tobymao/sqlglot) to parse BigQuery, Snowflake, DuckDB, Databricks, Redshift and Postgres SQL. Its `qualify()` step resolves columns against a supplied schema and reports unknown columns with their position.
- The official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (2.x, `MCPServer`).
- `uv` for environments and the lockfile, and `uvx scanisaur` as the install path.

## Consequences

- No other language has a parser that resolves columns across these dialects; TypeScript or Rust would mean building that layer.
- A quick test resolved small queries in 1–2 ms, well inside the latency budget.
- `qualify()` does not flag unknown *tables*, so Core does its own table lookup before qualifying.
- Python start-up is slower than a compiled binary. That doesn't matter for a long-running MCP server, but CLI start-up time should be watched.
