# BigQuery setup

Scanisaur reads BigQuery's catalog metadata (tables, columns, partitioning, clustering, keys, sizes) and never reads table data or runs the queries it checks ([ADR 0002](adr/0002-metadata-only-access.md)). It needs two roles, both granted on the project:

| Role | Why |
| --- | --- |
| `roles/bigquery.metadataViewer` | Lists datasets and tables, and reads the project-wide `INFORMATION_SCHEMA` views. A basic role such as Owner is denied those views ([spike 0001](spikes/0001-bigquery-metadata.md)) |
| `roles/bigquery.jobUser` | Runs the metadata queries |

Neither role can read table data ([spike 0003](spikes/0003-permissions.md)).

## Catalog-only service account

`scanisaur init` prints these commands with your project filled in:

```bash
SA=scanisaur-catalog@PROJECT.iam.gserviceaccount.com
gcloud iam service-accounts create scanisaur-catalog --project=PROJECT \
  --display-name="Scanisaur catalog-only"
for role in bigquery.metadataViewer bigquery.jobUser; do
  gcloud projects add-iam-policy-binding PROJECT --member="serviceAccount:$SA" \
    --role="roles/$role" --condition=None
done
gcloud auth application-default login --impersonate-service-account="$SA"
```

Credentials come only from Application Default Credentials, so no key file or secret goes in `scanisaur.yaml`. On a server, attach the service account to the machine or use Workload Identity Federation instead of the last line.

## Check it

```console
$ uv run scanisaur doctor
ok    credentials: 3 datasets in acme-analytics
ok    metadata: can read the project-wide INFORMATION_SCHEMA views
ok    data access: can't read table data
ok    cache: empty: the first check or `scanisaur refresh` fills it
```

`doctor` exits with 1 if any line isn't `ok`. It warns on data access when the account holds `bigquery.tables.getData` or `bigquery.tables.updateData` on any dataset, tested with the free `testIamPermissions` call, which reads no data. IAM changes can take several minutes to apply, so a warning right after removing a role may be out of date.

## What a refresh costs

A refresh runs four metadata queries. Each `INFORMATION_SCHEMA` view a query reads is billed at least 10 MiB; `__TABLES__` is billed nothing. Per-partition sizes are fetched only for partitioned tables of 10 GiB or more. The total is a few tens of MiB per dataset, under a cent at $6.25 per TiB. `doctor` runs one small query, billed 10 MiB.
