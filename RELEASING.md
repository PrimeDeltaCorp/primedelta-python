# Releasing

Releases are cut by pushing a version tag; CI builds, validates, and (once the
publish target is configured) publishes to PyPI.

## Versioning

Static version in `pyproject.toml` (`[project] version`). Bump it in a PR,
following semantic versioning, and tag the merge commit `vX.Y.Z` to match.

## Cut a release

1. Update `[project] version` in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. Merge that PR to `main`.
3. Tag and push:
   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```
4. The `Release` workflow (`.github/workflows/release.yml`) runs on the tag:
   - **build** — `python -m build` then `twine check dist/*` (fails the release
     if the metadata or README long-description won't render on PyPI).
   - **publish** — uploads to PyPI via **OIDC Trusted Publishing** (no API token).

## One-time publish setup (pending)

The `publish` job has a **hard guard**: `if: vars.PYPI_PUBLISH_ENABLED == 'true'`.
Until that repo variable is set to `true`, publish is **skipped** and a tag only
builds and validates artifacts — safe today. (A GitHub Environment alone does
NOT gate: a referenced-but-absent environment is auto-created rule-free and the
job runs, so the variable guard is what actually protects you.)

To enable publishing, all of:

1. ✅ **License** — MIT (`LICENSE`), publication approved.
2. A PyPI project `primedelta` exists with a **Trusted Publisher** pointing at
   this repo + the `Release` workflow + the `pypi` environment
   (PyPI → project → Publishing → Add a GitHub Actions publisher).
3. The `pypi` Environment is created in repo settings **with required reviewers**
   so each release needs a human sign-off before upload.
4. The repo variable `PYPI_PUBLISH_ENABLED` is set to `true`
   (Settings → Secrets and variables → Actions → Variables).

Until step 4, a tag produces validated build artifacts and the publish job is
skipped.
