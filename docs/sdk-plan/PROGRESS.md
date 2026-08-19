# PrimeDelta Python SDK — Progress Tracker

> Living status log for the SDK revive effort. Update at the end of each work session.
> Plan: `02-plan.md` · Findings: `01-recon-findings.md` · Baseline: `00-current-inventory.md`.

## Status: PLANNED — decisions made; implementation must start in a session launched INSIDE primedelta-python

**Blocker to start coding:** the planning session runs in a `dex-frontend` worktree and the harness
hard-blocks Write/Edit + git against the sibling `primedelta-python` repo (subagents inherit this).
→ Relaunch Claude Code in `~/primedelta/primedelta-python` (or a worktree of it) to implement.

| Phase | Title | State |
| --- | --- | --- |
| Recon | Inventory + gap map (5-agent) | ✅ done 2026-08-18 |
| 0 | Validate & un-break the core | ⬜ not started (blocked on direction) |
| 1 | Signer abstraction | ⬜ not started |
| 2 | Headless provisioning (keystore/mnemonic/KMS) | ⬜ not started |
| 3 | MetaMask bridge (BrowserSigner) | ⬜ not started |
| 4 | Feature completeness (backend + on-chain gaps) | ⬜ not started |
| 5 | Multi-network (testnet/mainnet) | ⬜ not started |
| 6 | Test hardening + drift test | ⬜ not started |
| 7 | Packaging, CI, docs, publish | ⬜ not started |

## Decisions log
- 2026-08-18: Recon complete. Verdict = **not delete** — breakage is concentrated in the stale
  auth/client layer (backend moved to cookie+CSRF), not the architecture.
- 2026-08-18 (PO): **Direction = keep good modules + rewrite rest + restructure package.**
  **Publish = public PyPI + relicense** (Apache-2.0 proposed, legal to confirm).
  **Networks = dev → testnet now, mainnet last.**

## Open blockers
- [x] PO: direction — decided (keep+rewrite+restructure).
- [x] PO: publish target — decided (public PyPI + relicense).
- [ ] Exact license text (Apache-2.0 vs MIT) — PO/legal, before publish only (non-blocking).
- [ ] Relaunch a session inside `primedelta-python` to begin Phase 0.

## Phase 0 kickoff checklist (for the primedelta-python session)
1. `git checkout -b feat/sdk-revive` off `main` (@ d36930f).
2. Move planning docs are already in `docs/sdk-plan/` (00/01/02/PROGRESS) — commit them first.
3. Start §Restructure: create `signers/`, `client/`, `chain/` skeletons; move salvaged modules.
4. Rewrite `client/session.py` onto `requests.Session` + CSRF; prove `login()`+`portfolio()` on dev.
   Dev creds: verified+DID test wallets are in memory `reference_dev_test_wallets` (keys public).

## Session notes
- 2026-08-18: read full source; 5-agent recon; wrote 00/01/02 + this tracker. Key runtime break
  found: SDK on stale header-token auth vs backend cookie `dclex_auth`+CSRF; `/users/verify/` now
  204 no-body. Suspected web3-v7 `rawTransaction` break to verify in Phase 0.
