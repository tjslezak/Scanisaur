# Contributing to Scanisaur

Thanks for helping. Scanisaur is in early development, so expect things to move.

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

You need [uv](https://docs.astral.sh/uv/). It installs the right Python and all dependencies.

```bash
git clone https://github.com/tjslezak/Scanisaur.git
cd Scanisaur
uv sync
uv run pre-commit install
```

## Checks

CI runs these on every pull request; run them locally first:

```bash
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest --cov
```

`pre-commit` runs ruff, mypy and a private-key check on each commit.

## Credentials

Never put credentials in the repository. To work against BigQuery from your machine, impersonate a service account with your own Google login instead of downloading a key:

```bash
gcloud auth application-default login \
  --impersonate-service-account=SERVICE_ACCOUNT_EMAIL
```

If you must keep a key file, store it outside the repository (for example under `~/.config/`) and readable only by you. Three checks catch mistakes:

- `.gitignore` skips common key file names.
- The `detect-private-key` pre-commit hook, which CI also runs, rejects any file that contains a private key.
- GitHub push protection blocks pushes that contain known credential formats.

If a key is ever committed, delete it in Google Cloud first. Rewriting Git history doesn't make it secret again.

## Pull requests

- Keep each pull request to one change, with tests.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages, for example `feat(rules): add SCN011`.
- Significant design choices get an architecture decision record in [`docs/adr/`](docs/adr/).

## Reporting false positives

If Scanisaur flags a query that was fine, misses a real problem, or estimates cost badly, open a **False positive** issue. These reports are how the rules improve. Remove sensitive literal values and names first.

## Security

Don't report vulnerabilities in public issues; see [SECURITY.md](SECURITY.md).

## License

Scanisaur is licensed under the [Apache License 2.0](LICENSE). Under section 5 of that license, contributions you submit are licensed under the same terms.
