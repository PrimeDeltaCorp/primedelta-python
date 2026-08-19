# PrimeDelta Python SDK — Recon Findings & Reconciliation

> Synthesis of a 5-agent recon (git-state, tests+packaging, backend-API-surface,
> contract-surface, wallet-connection). Full agent reports archived in the workflow
> output. Written 2026-08-18.

## 0. The reconciliation — "outdated / nothing works" vs. "code looks recent & polished"

Both are true, and they don't contradict once you separate **code age** from **runtime validity**:

- **Git facts:** repo `PrimeDeltaCorp/primedelta-python` (PUBLIC), on `main` @ `d36930f`,
  clean, 0 ahead/0 behind origin. **Only 4 commits ever**: `Initial commit` → 2 Dependabot
  dep bumps → "park public Pyth stream". So the entire SDK body landed in the *initial commit*
  and has had **no feature/maintenance work since** — only dependency bumps. "Unmaintained
  since ~March" is accurate; the code merely *looks* current because the initial commit was
  already sophisticated (cross-dex, AMMT, WDEL, rich Besu handling).
- **Runtime facts (why it doesn't work):** the backend moved on underneath it. The SDK is
  built on an **auth contract the backend no longer honors**, plus stale endpoints and a
  likely web3-v7 breakage. So it compiles and reads well but **fails end-to-end against the
  live stack.** See §1.

**Conclusion: not a delete candidate — a REVIVE candidate.** The breakage is concentrated in
the client/auth layer + config; the hard on-chain code (DEX handlers, nonce/gas/revert logic,
pool resolution) is sound and expensive to reproduce. A from-scratch rewrite would re-derive
all the good parts and re-hit the same Besu gotchas. Recommendation: **keep architecture,
rewrite the broken auth/client layer, refresh config, add the missing pieces.**

## 1. Why it doesn't work today (systemic, concentrated, fixable)

### 1a. Auth model is fully stale — HIGHEST IMPACT (breaks login + everything authed)
Backend now authenticates **only** via the HttpOnly cookie `dclex_auth` set by
`POST /users/verify/` (which now returns **204, no JSON body**) and enforces **CSRF** on unsafe
methods (`backend/dclex/dclex/django_app/authentication.py`, `settings.py`). The SDK:
- `login()` reads `response.json()["token"]` from `/users/verify/` → **no body → breaks**.
- every `_authorized_*` call sends `Authorization: Token <t>` → **header ignored**, backend
  reads the cookie → the SDK authenticates as **anonymous**.
- no CSRF token fetched/sent → unsafe cookie-auth calls rejected.
→ **Fix:** rewrite `PrimeDeltaClient` to a `requests.Session` (cookie jar) + fetch/attach
`X-CSRFToken` from `GET /csrf-token/`; drop the header/token model. This is the linchpin.

### 1b. Signed prices auth stale → **swaps can't fetch prices** (breaks DEX)
`/signed-prices/` authenticates off the `dclex_auth` cookie **and requires KYC VERIFIED**;
SDK sends `Authorization: Bearer` (ignored) → 401. Fixed by 1a (cookie session).

### 1c. Price stream token stale → broker stream won't resolve
`/prices-stream/` now needs a short-lived token from `GET /prices-stream-token/`; SDK passes
the login token. Fix: mint via `/prices-stream-token/` first.

### 1d. Stablecoin withdrawal on legacy route
SDK posts `/initialize-usdc-withdraw/` (hardcodes `asset_type="USDC"`). dUSD path is
`/initialize-stablecoin-withdraw/` `{amount, symbol:"dUSD"}` (+ `/deposit-stablecoin-signature/`
for deposits) gated by `DUSD_ENABLED`. Fix: migrate to dUSD routes.

### 1e. Suspected web3-v7 break (verify in Phase 0)
`primedelta.py:~832` uses `signed_transaction.rawTransaction`; web3 v7 / eth-account renamed it
to `raw_transaction`. If the alias was removed, **every local-key tx submission AttributeErrors**
— another "nothing works" signal even on the raw-key path. Verify against the pinned
`web3==7.15.0` and standardize on `raw_transaction`.

### 1f. Stale network config (only `dev`, addresses drift every redeploy)
Only `networks/dev.json` (chain 2028) ships; dev addresses change on every chain redeploy, so
even the on-chain paths point at stale bytecode/ABIs unless refreshed. No testnet(7357)/
mainnet(4109) configs at all.

## 2. Backend API surface — what the SDK does NOT wrap (proposed additions)

High value:
- **User messages** — `GET /messages/`, `POST /messages/{id}/` (mark-read). Newest surface
  (the admin→user messages we shipped). Unwrapped.
- **Fiat** — `GET /user/bank-details/` (deposit instructions + per-user reference code),
  `POST /fiat-withdrawals/` (bank withdrawal). Unwrapped.
- **dUSD native flows** — `POST /initialize-stablecoin-withdraw/`, `POST /deposit-stablecoin-signature/`.
- **Cost/quote endpoints** — `/orders/limit/{buy,sell}/cost/`, `/orders/market/sell/cost/`
  (fee/total preview before submit).
- **Cookie-auth prerequisites** — `GET /me/`, `GET /csrf-token/`, `GET /prices-stream-token/`,
  `GET /swappable-symbols/`.

Medium: `GET /portfolio/history/`, `GET /stocks-to-be-deposited/`, `GET /transfer-calculator/`,
`GET /digital-identity/` (tokenId) + `GET /digital-identity-signature/`, recovery
(`/recovery-token/`, `/recovery-signature/`), `POST /verification-token/` (start KYC),
`GET/POST /source-of-funds/`, `GET /application-settings/`, `GET /contracts/`,
`POST /report-issue/`, SCT/AMM (`/initialize-smart-contract-token/`, `/smart-contract-tokens/`,
`/smart-contract-token-signature/{addr}/`, `/v3/pools/{addr}/tick-density/`).

**NOT gaps (don't exist in backend — don't build):** market BUY order (only market SELL exists),
referrals (no endpoint), standalone dividend claim (dividends are custodial auto-settled
`type=DIVIDEND` rows in `/closed-distributions/`, already wrapped), account registration
(auto-provisioned on first authed request).

## 3. On-chain surface — gaps (proposed additions)

- **Quoting / preview = the single biggest UX gap.** No pre-trade or pre-LP preview at all;
  caller must supply `min_out`/`max_in`/`amount*_min` with no SDK help.
  - AMM: bundle + wire **Uniswap V3 QuoterV2** (`quoteExactInput/OutputSingle`); expose
    `univ3_pool.slot0()` for spot price. Neither is present today.
  - PRICE_FEED (DclexPool): no `getAmountOut` view in bundled ABI; needs a contract quote path
    or off-chain oracle-price + fee-curve math (oracle ABI has **no price getter** — would need
    a price-read ABI added).
  - Route-level quote must quote per-leg via `router.getPoolType(token)` (unexposed).
- **Allowance management** — no read `allowance` / set standing approval / revoke (`approve(…,0)`);
  the SDK silently leaves approvals behind.
- **DID reads** — `getId/ownerOf/isValid/isPro/tokenURI` unexposed; recovery-state getter is
  **absent from the bundled DID ABI** (would need ABI update upstream).
- **V3 position lifecycle** — `increaseLiquidity` (top-up) + `burn` (retire) missing.
- **LP fee preview** — `lp_position()` returns stale `tokensOwed`; real claimable needs a
  static-`collect` sim or `feeGrowthInside` math.
- **Native DEL send** — read/wrap/unwrap exist, but no bare value-transfer helper.

## 4. Tests / packaging / publishing readiness

- **No CI whatsoever** — `.github/` absent. Nothing runs pytest/black/isort/mypy on push.
- **No `py.typed`** — code is fully typed but types are NOT delivered to consumers (PEP 561).
- **Unit-test blind spot:** the entire `PrimeDeltaClient` HTTP layer (request building,
  camelCase→dataclass parsing, error mapping) is untested except the two Pyth helpers; success
  paths of deposits/withdraw-claims/`get_signed_price_updates` untested. Many `primedelta.py`
  paths untested (nonce-retry loop, `swap_token_to_token_*` wrappers, lp reads, `slot0` n/a).
- **Integration bootstrap assumes a local anvil** (hardcoded deployer key funds/mints) — it's a
  local-stack helper, not a dev/testnet/mainnet validator.
- **`python -m build` works.** But:
- **⚠️ LICENSE BLOCKS PUBLIC PYPI.** The bundled license is a custom **"Prime Delta API
  Non-Commercial License"** that prohibits redistribution (§4.3 "not publish, disseminate, or
  redistribute"). Publishing to public PyPI *is* redistribution → **direct conflict with the
  DoD ("published package").** Must be resolved by the PO (change license, or publish to a
  private index, or git-install only). **BLOCKING DECISION.**
- **Hard-pinned runtime deps** (`web3==7.15.0`, …) — unusual for a library; will cause resolver
  conflicts for consumers. Loosen to compatible ranges (`>=,<`).
- **Split/inconsistent dev deps** (pyproject `dependency-groups.dev` vs `requirements-dev.txt`
  disagree on pytest; black/isort/mypy only in the txt). No `[tool.black/isort/mypy]` config.

## 5. Wallet connection — see 02-plan §Wallet. Summary: introduce a `Signer` protocol
(3 needs: `address`, `sign_message`, `submit_transaction` + a `fills_gas_and_nonce` flag);
ship `LocalAccountSigner` (key/keystore/mnemonic), `KmsSigner` (prod, key never leaves HSM),
`BrowserSigner` (local loopback bridge = the answer to "plug in MetaMask", NOT WalletConnect),
+ `MockBrowserSigner`/anvil-impersonation for CI. `private_key=` stays as a thin back-compat
wrapper. Refactor footprint is tiny (~3 lines in login/init + one branch in the tx builder).
