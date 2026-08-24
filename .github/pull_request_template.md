<!-- Keep the title Conventional Commits: feat: / fix: / chore: / docs: / test: … -->

## What & why


## Signing / safety (if this touches trades, signers, or craft)
- [ ] A new fund-moving path goes through a `Signer` (local / KMS / browser) — no key is logged or hard-coded
- [ ] `craft()` still captures (never broadcasts) for every action that produces an on-chain tx
- [ ] Struct args stay ABI-encodable under `craft()` (dict is fine — the encoder handles it)

## Verification
- [ ] `pytest` passes (3.10 / 3.11 / 3.12)
- [ ] `mypy src/primedelta` / `black --check` / `isort --check-only` clean
- [ ] Cold-reviewed in a fresh clone (or noted why not)
- [ ] CHANGELOG updated under **Unreleased** for a user-facing change
