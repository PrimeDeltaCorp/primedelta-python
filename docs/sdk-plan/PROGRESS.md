# PrimeDelta Python SDK — Progress Tracker

> Living status log for the SDK revive effort. Update at the end of each work session.
> Plan: `02-plan.md` · Findings: `01-recon-findings.md` · Baseline: `00-current-inventory.md`.

## Status: PLAN COMPLETE (2026-08-20) — all executable phases merged; only externally-blocked items remain (license/publish, mainnet re-deploy, testnet KYC wallet)

| Phase | Title | State |
| --- | --- | --- |
| Recon | Inventory + gap map (5-agent) | ✅ done 2026-08-18 |
| 0 | Validate & un-break the core | ✅ merged #5 (reality gate PASS on dev) |
| — | On-chain fixes (withdraw/deposit/AMM) | ✅ merged #6 (adversarial-audit driven; live round-trip) |
| 1 | Signer abstraction | ✅ merged #7 |
| 2 | Headless provisioning (keystore/mnemonic/KMS) | ✅ merged (LocalAccountSigner from_keystore/from_mnemonic, KmsSigner) |
| 3 | MetaMask bridge (BrowserSigner) | ✅ merged (loopback bridge + MockBrowserSigner) |
| 4 | Feature completeness (backend + on-chain gaps) | ✅ merged #13–#18 (messages/fiat/cost/helpers, dUSD burn voucher, allowances, DID reads, quoting, V3 lifecycle, send_del) |
| 5 | Multi-network | ✅ testnet #19 (on-chain-verified) + per-network endpoints/SIWE #23; ⛔ **mainnet.json blocked** — see below |
| 6 | Test hardening + drift test | ✅ client transport tests #20, live-contract-drift guard #21, env-aware happy-path #24, coverage gate 85% #25 |
| 7 | Packaging, CI, docs, publish | ✅ py.typed/metadata/CHANGELOG #22, examples #26, README+CONTRIBUTING #27, isort+mypy gates #30, gated release pipeline #31; ⛔ **publish blocked on license** |
| 5.3 | Config-refresh runbook | ✅ merged #32 |

### Externally blocked (not code — need a decision/access/redeploy)
- **7.3 License / publish (7.6/7.8):** package is not published under the current
  non-commercial LICENSE. Release infra is armed — the `Release` workflow builds +
  `twine check`s on a `v*` tag and publishes via OIDC once the license is chosen,
  a PyPI Trusted Publisher is configured, and the `PYPI_PUBLISH_ENABLED` repo
  variable is set to `true`. See `RELEASING.md`.
- **mainnet.json:** intentionally NOT shipped. The public `chain.primedelta.io`
  RPC serves a **re-genesised** chain 4109 (block ~16k on 2026-08-20, well below the
  deployment's `initial_block` 27352) where every documented mainnet address has
  zero bytecode — i.e. the 2026-07-13 deploy is on an abandoned chain state. Needs
  ops to re-deploy (or confirm the correct RPC + `addresses.json`), then run the
  generator (`docs/refresh-network-config.md`).
- **Full testnet authed flow:** endpoint/SIWE resolution + read paths verified;
  a complete login→order→swap needs a VERIFIED_MINTED testnet wallet.

### Follow-ups (small, non-blocking)
- Local-Anvil integration bootstrap still references a removed `_token` attr
  (cookie-auth migration fallout) — unreachable on dev/testnet; fix when a local
  Anvil stack is available.
- Tighten mypy from the current lenient config toward strict, incrementally.

**Phase 1 (Signer) notes:** `signer.py` = `Signer` Protocol + `LocalAccountSigner` (`from_key`). `PrimeDelta(signer=)`;
`private_key=` is now a thin back-compat wrapper. `login()` and the tx builder go through `self._signer`
(branch on `fills_gas_and_nonce`); handlers unchanged (`.address` only). Cold review clean; 131 tests; live dev
verified (signer= construct + login + wrap/unwrap DEL). **Phase 3 carry-over:** the `fills_gas_and_nonce=True`
path is dormant — a wallet signer must NOT rely on `contract_function.build_transaction({from,value})` (web3
auto-estimates gas + fills nonce during build, defeating "wallet fills its own" and re-hitting the Besu nonce
race). Build the wallet tx from raw calldata/to/value instead when `BrowserSigner` lands.

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
- [x] Relaunch a session inside `primedelta-python` to begin Phase 0 — done; Phase 0 complete.

## Phase 0 kickoff checklist (for the primedelta-python session)
1. `git checkout -b feat/sdk-revive` off `main` (@ d36930f).
2. Move planning docs are already in `docs/sdk-plan/` (00/01/02/PROGRESS) — commit them first.
3. Start §Restructure: create `signers/`, `client/`, `chain/` skeletons; move salvaged modules.
4. Rewrite `client/session.py` onto `requests.Session` + CSRF; prove `login()`+`portfolio()` on dev.
   Dev creds: verified+DID test wallets are in memory `reference_dev_test_wallets` (keys public).

## Phase 0 results (2026-08-19)
Reproduced every break on live dev, fixed them, and proved the core with the SDK itself.

**Root causes found (live-verified):**
1. **SIWE domain wrong** (new, recon missed it). SDK signed `domain=app.dev.primedelta.io`;
   dev backend `SIWE_DOMAIN=mint.dev.primedelta.io,dex-dev...,validator-dev...,localhost:5173`.
   Off the allowlist → backend forces `domains[0]` → `DomainMismatch` → `MESSAGE_VERIFICATION_ERROR`
   (400) on `/users/verify/` for BOTH sig formats. This blocked login before the token/cookie issue
   even mattered. Fix: `settings.PRIMEDELTA_APP_URL` default → `https://mint.dev.primedelta.io`
   (Phase 5 makes SIWE domain per-network).
2. **Auth = cookie `dclex_auth` + CSRF** (confirmed). `/users/verify/` → 204, sets HttpOnly
   `dclex_auth`. Authed calls read the cookie; `Authorization: Token/Bearer` ignored. Unsafe methods
   need `X-CSRFToken` (from `GET /csrf-token/` body) **AND** a `Referer`/`Origin` header whose host ==
   request host (Django strict-referer on HTTPS — the non-obvious extra requirement). Rewrote
   `PrimeDeltaClient` onto a `requests.Session`: cookie jar + lazy CSRF + `Origin`/`Referer` on unsafe
   + robust error mapping (400 `errorCode`|`code`, 401→NotLoggedIn, 403→AuthorizationError). Added `me()`.
3. **web3-v7 `rawTransaction`** (confirmed). eth-account 0.13.7 `SignedTransaction` has
   `raw_transaction`, not `rawTransaction` → every local-key tx AttributeError'd. Fixed
   `primedelta.py` send path. Proven by a real mined tx on dev.
4. **All 8 dev.json addresses stale** (2026-07-30 redeploy). Refreshed from
   `blockchain/deployments/primedelta-dev/addresses.json`; added `scripts/generate_network_config.py`
   to regenerate config from any deployment (reused in Phase 5).
5. **dUSD withdrawal route** migrated `/initialize-usdc-withdraw/` → `/initialize-stablecoin-withdraw/`
   `{amount, symbol:"dUSD"}` (DUSD_ENABLED=true on dev). Signed-prices now over the cookie session
   (no Bearer); prices-stream token minted via `GET /prices-stream-token/`.
6. **Portfolio null field**: `profitLossPercentage` is null for AMM tokens (avg price 0) → parser
   crashed. Made `Position.profit_loss_percentage` `Optional[Decimal]`.

**Reality gate (0.9) — PASS on dev** (wallet = ADMIN `0x7099…79C8`, VERIFIED_MINTED, DID 47):
login → `logged_in()`/`me()` → `portfolio()` (AMMT1 20.00) → `stocks()` (45) →
`swap_exact_input("AMMT1", STABLECOIN_TO_STOCK, 1 dUSD, min_out=0)` → tx mined
(`0x8e0953…21fdbd`), on-chain delta **dUSD −1.000000 / AMMT1 +0.098005**. Swap took the AMM (V3)
pool path (market-hours- and signed-price-independent — AMMT1/dUSD pool has liquidity; PRICE_FEED
stocks need signed oracle prices which are empty outside US market hours).

**Unit suite:** 94 passed (transport-agnostic — they mock `PrimeDeltaClient` methods, not `requests`).

**Not exercised live (verified by source-trace only, no state change):** dUSD withdrawal init,
deposit-signature, full SSE consumption. Deposit-stablecoin-signature stays on the raw-transfer path
for now → Phase 4.2.

## Session notes
- 2026-08-18: read full source; 5-agent recon; wrote 00/01/02 + this tracker. Key runtime break
  found: SDK on stale header-token auth vs backend cookie `dclex_auth`+CSRF; `/users/verify/` now
  204 no-body. Suspected web3-v7 `rawTransaction` break to verify in Phase 0.
- 2026-08-19: Phase 0 executed (see above). Branch `feat/sdk-revive` off `main@d36930f`. Package
  restructure (§Restructure: signers/ client/ chain/ models/) deferred — kept Phase 0 diff focused on
  the reality gate; restructure lands with Phase 1 (Signer seam) to keep each PR reviewable.
