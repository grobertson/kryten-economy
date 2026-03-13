# Releasing Kryten Economy

This project now publishes to PyPI through GitHub Actions using trusted publishing (OIDC).

Primary workflow file:

- `.github/workflows/publish.yml`

## Release model

- Publish runs on pushed tags that match `v*`.
- Manual run is also available via `workflow_dispatch`.
- The workflow validates that the pushed tag matches the package version in `pyproject.toml`.

In practice:

- `pyproject.toml` has `version = "0.8.8"`
- release tag must be `v0.8.8`

If they do not match, CI fails before publish.

## Standard release steps

1. Ensure branch is clean and up to date.
2. Update version in `pyproject.toml`.
3. Add changelog entry in `CHANGELOG.md`.
4. Commit and push to `main`.
5. Create and push matching annotated tag:

```bash
git tag -a v0.8.8 -m "Release v0.8.8"
git push origin v0.8.8
```

6. Watch GitHub Actions `Publish to PyPI` run.
7. Verify release on PyPI:

```bash
python - <<'PY'
import requests
print(requests.get("https://pypi.org/pypi/kryten-economy/json", timeout=30).json()["info"]["version"])
PY
```

## Optional manual publish run

If needed, you can trigger the workflow manually from GitHub Actions (`workflow_dispatch`).

Notes:

- Manual dispatch still builds and publishes using the current repository state.
- Tag/version mismatch guard only runs on tag push events.

## Troubleshooting

- Workflow fails with tag/version mismatch:
	- Ensure tag is exactly `v` + package version.
- Publish says version exists:
	- That version is already on PyPI; bump patch version and tag again.
- No publish run after push:
	- Confirm you pushed a tag (not just `main`) and that tag starts with `v`.