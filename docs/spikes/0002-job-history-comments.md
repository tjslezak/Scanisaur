# Spike 0002: does BigQuery job history keep SQL comments?

- **Issue:** [#2](https://github.com/tjslezak/Scanisaur/issues/2)
- **Date:** 2026-10-01
- **Status:** Done

## Question

Does the tracking tag `/* scanisaur:chk_<id> */` survive into `region-<location>.INFORMATION_SCHEMA.JOBS_BY_PROJECT.query`, whichever way the agent runs its query?

## Answer

Yes, on every path tested, whether the tag leads or trails the query. Matching checks to executed queries can rely on the tag, so the fallback (fingerprint plus a time window) isn't needed.

| Path | Leading tag kept | Trailing tag kept | Labels on the job |
| --- | --- | --- | --- |
| `bq` CLI | Yes | Yes | Only the caller's |
| Python client (`google-cloud-bigquery`) | Yes | Yes | Only the caller's |
| Google's BigQuery MCP server (`execute_sql`) | Yes | Yes | `goog-mcp-server=true`, plus the caller's |

> **Update (issue [#13](https://github.com/tjslezak/Scanisaur/issues/13)):** the tag is now `/* scanisaur:q_<fingerprint> */`. The fingerprint is a hash of the SQL's tokens, ignoring whitespace and comments, so the same query always gets the same tag. A tag unique to each check made every repeated query miss BigQuery's cache: the same query with a different comment was billed in full each time, and an identical one was free. Matching works as before, except that a tag now identifies the query rather than one check; each check keeps its own `check_id`.

## Details

- **MCP queries without a job:** the MCP server runs fast queries without creating a persistent job, and returns a query ID instead of a job ID. They still appear in `JOBS_BY_PROJECT`, with the query ID as `job_id`.
- **Finding agent queries:** the `goog-mcp-server=true` label marks every query the MCP server runs, so `scanisaur audit` can select them with `EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'goog-mcp-server')`.
- **Rejected MCP queries aren't recorded:** when the MCP server sends a query BigQuery rejects before running (for example, one with no filter on a table that requires one), no job is created and nothing appears in `JOBS_BY_PROJECT`. Audit can't see it; the agent's own tool call is the only record. `bq` and the Python client create the job first, so their rejected queries do appear, as failed jobs with `error_result.reason` set (for example, `accessDenied`).
- **Permission:** reading `JOBS_BY_PROJECT` needs `bigquery.jobs.listAll` at the project level. The catalog account got it from `roles/bigquery.resourceViewer`. The planner account, without it, was denied: `User does not have the required permissions ('bigquery.jobs.listAll' permission(s) at the project level)`.
- **Sensitivity:** job history holds the full text of every query in the project, including literal values such as email addresses in `WHERE` clauses. Granting `jobs.listAll` exposes that text even though it grants no table data. The docs for `scanisaur audit` must say so.
