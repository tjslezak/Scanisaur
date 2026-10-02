# Spike 0003: permissions for catalog-only access and planner mode

- **Issue:** [#3](https://github.com/tjslezak/Scanisaur/issues/3)
- **Date:** 2026-10-01
- **Status:** Done, except one follow-up check (below)

## Question

Which roles give Scanisaur catalog metadata without table data, which does planner mode need for dry runs, and how can `scanisaur doctor` tell whether a role can read data without reading any?

## Answer

- **Catalog-only access works.** The catalog account reads table metadata and partition metadata, and is denied both `SELECT` and `tabledata.list`.
- **A dry run needs data-read permission.** The catalog account's dry run was denied with the same error as a real query. So an account that can dry-run a query can also read its data, as [ADR 0002](../adr/0002-metadata-only-access.md) warns.
- **Planner mode can be limited to chosen datasets** by granting `roles/bigquery.dataViewer` on each dataset instead of on the project.
- **`doctor` should use `testIamPermissions`.** It reports which permissions an account holds on a table without touching data. The catalog account got `bigquery.tables.get` only; the planner got `bigquery.tables.get` and `bigquery.tables.getData`.

## Results

Tested on a one-row private table, as two service accounts:

- the catalog account, with `metadataViewer`, `jobUser` and `resourceViewer`;
- the planner account, with `jobUser` and `dataViewer`.

| Action | Catalog account | Planner account |
| --- | --- | --- |
| `bq show` (table metadata) | Allowed | Not tested |
| `INFORMATION_SCHEMA.PARTITIONS` | Allowed | Not tested |
| `tabledata.list` (`bq head`) | Denied: `Permission bigquery.tables.getData denied` | Not tested |
| `SELECT` | Denied: `User does not have permission to query table` | Not tested |
| Dry run | **Denied**, same error as `SELECT` | Allowed: 37 bytes |
| `testIamPermissions` for `tables.get`, `tables.getData`, `tables.updateData` | `tables.get` | `tables.get`, `tables.getData` |
| Dry run after moving `dataViewer` from the project to the table's dataset | Not tested | Allowed: 37 bytes |
| Dry run on another dataset after that move | Not tested | **Allowed, but expected denied.** See the follow-up below |

## Follow-up

After the planner's project-level `dataViewer` was removed, a dry run on a dataset it should no longer read still validated. The planner holds no role that makes it a project reader, so the likeliest cause is IAM propagation: the test waited 60 seconds, and removing a binding can take several minutes. To settle it, run these after a few minutes. The first should print only `roles/bigquery.jobUser`, and the second should fail with "Access Denied":

```bash
gcloud projects get-iam-policy PROJECT --flatten=bindings \
  --filter="bindings.members:scanisaur-planner" --format="value(bindings.role)"
CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT=scanisaur-planner@PROJECT.iam.gserviceaccount.com \
  bq --project_id=PROJECT query --use_legacy_sql=false --dry_run \
  'SELECT id FROM PROJECT.spike_many.t_0001'
```

## Profiles for `scanisaur init`

**Catalog-only (the default).** `metadataViewer` covers the API calls in [spike 0001](0001-bigquery-metadata.md). `jobUser` is needed only for INFORMATION_SCHEMA queries, and `resourceViewer` only for `scanisaur audit` to read job history:

```bash
SA=scanisaur-catalog@PROJECT.iam.gserviceaccount.com
gcloud iam service-accounts create scanisaur-catalog --project=PROJECT \
  --display-name="Scanisaur catalog-only"
for role in bigquery.metadataViewer bigquery.jobUser; do
  gcloud projects add-iam-policy-binding PROJECT --member="serviceAccount:$SA" \
    --role="roles/$role" --condition=None
done
# Optional, for scanisaur audit. This exposes the text of every query in the project.
gcloud projects add-iam-policy-binding PROJECT --member="serviceAccount:$SA" \
  --role=roles/bigquery.resourceViewer --condition=None
```

**Planner mode (opt-in).** The planner account can read the data in every dataset it is granted, so grant only the datasets the agent queries:

```bash
SA=scanisaur-planner@PROJECT.iam.gserviceaccount.com
gcloud iam service-accounts create scanisaur-planner --project=PROJECT \
  --display-name="Scanisaur planner"
for role in bigquery.metadataViewer bigquery.jobUser; do
  gcloud projects add-iam-policy-binding PROJECT --member="serviceAccount:$SA" \
    --role="roles/$role" --condition=None
done
# One line per dataset the agent queries.
bq query --use_legacy_sql=false \
  "GRANT \`roles/bigquery.dataViewer\` ON SCHEMA \`PROJECT.DATASET\` TO 'serviceAccount:$SA'"
```

## `doctor` detection

For each dataset, `doctor` calls `tables.testIamPermissions` on one table with `bigquery.tables.getData` and `bigquery.tables.updateData`:

- **Catalog-only mode:** warn if either permission is granted.
- **Planner mode:** report which datasets grant `getData`, since that is the data the planner can read.

The call is free and reads no data. Dataset-level grants apply to every table in the dataset, so one table per dataset is enough unless table-level grants are in use; then test every table, one call each, like a `tables.get` refresh.
