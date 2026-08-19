# Contributing

Thanks for helping improve the PrimeDelta Python SDK.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[kms]" pytest pytest-cov black==26.3.1
```

The package layout:

- `src/primedelta/` — the SDK (façade, client, signers, on-chain handlers, bundled network config).
- `tests/` — unit tests (mock everything, no network) and `tests/integration/` (live, self-skipping).
- `examples/` — runnable usage samples.

## Tests

Unit tests run offline and are the default gate:

```bash
pytest tests --ignore=tests/integration --cov=primedelta --cov-report=term-missing
```

Coverage is gated at **85%** (`[tool.coverage.report] fail_under`).

Integration tests hit a live backend + chain and **self-skip** without credentials. To run them against dev, export:

```bash
export PRIMEDELTA_PROVIDER_URL=https://besu-dev.primedelta.io
export PRIMEDELTA_TEST_PRIVATE_KEY=0x...        # a funded, VERIFIED_MINTED wallet
pytest tests/integration -m integration
```

`network=` selects the backend + SIWE domain automatically (dev/testnet); the `PRIMEDELTA_BASE_URL` / `PRIMEDELTA_APP_URL` env vars override for local stacks.

## Formatting

Code is formatted with **black** (line length 88), enforced in CI:

```bash
black src tests scripts        # format
black --check src tests scripts
```

`isort` and `mypy` config ship in `pyproject.toml` but are not yet CI-gated.

## Pull requests

- One focused change per PR; keep the diff reviewable.
- Green unit suite + `black --check` before pushing.
- Add or update tests for behavior changes — a test should fail if the fix is reverted.
- Use Conventional-Commit PR titles (`feat:` / `fix:` / `chore:` / `test:` / `docs:` …).
- Don't commit secrets. Dev keys are public/deterministic but must never touch testnet/mainnet.
