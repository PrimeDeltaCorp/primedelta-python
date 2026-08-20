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

The `publish` job is gated on a GitHub Environment named `pypi`, so tagging is
safe today — it builds and checks the artifacts but publishes nothing until:

1. The **license** is finalized (see the plan / `LICENSE`) — the package is not
   published under the current non-commercial license.
2. A PyPI project `primedelta` exists with a **Trusted Publisher** pointing at
   this repo + the `Release` workflow + the `pypi` environment
   (PyPI → project → Publishing → Add a GitHub Actions publisher).
3. A `pypi` Environment is created in repo settings (optionally with required
   reviewers) so releases are approved before upload.

Until then a tag produces validated build artifacts and the publish job waits on
the (absent) environment.
