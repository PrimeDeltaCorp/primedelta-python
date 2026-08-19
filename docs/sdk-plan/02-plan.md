# PrimeDelta Python SDK — Implementation Plan & TODOs

> Companion to `00-current-inventory.md` and `01-recon-findings.md`. Written 2026-08-18.
>
> **DECIDED (PO, 2026-08-18):**
> 1. **Direction = keep the good modules + rewrite the rest + restructure the package.** Salvage
>    the on-chain code (DEX handlers, param dataclasses, ABIs, Besu tx/nonce/gas/revert logic,
>    contracts/networks loader, domain types); **rewrite from scratch** the backend client + auth
>    layer (cookie+CSRF) and reorganize the package into clear subpackages (see §Restructure).
> 2. **Publish target = public PyPI + relicense.** The non-commercial no-redistribution LICENSE
>    is replaced with a redistribution-permitting license (proposed **Apache-2.0** for the patent
>    grant, or MIT — final choice needs PO/legal sign-off before the actual publish; not a build
>    blocker).
> 3. **Networks = dev → testnet now**, mainnet config last (writes only on explicit go).

## Restructure (target layout)
Keep the salvaged on-chain code; isolate the rewritten layer; split the `primedelta.py` god-object.
```
src/primedelta/
  __init__.py            # curated public API
  primedelta.py          # slim façade wiring the pieces below
  signers/               # NEW — Signer protocol + impls
    base.py  local.py  kms.py  browser.py (+ bridge page)  mock.py
  client/                # NEW (rewritten) — backend REST layer
    session.py           # requests.Session: cookie jar + CSRF + error mapping
    resources.py         # endpoint methods returning models  (or split per domain)
  chain/                 # KEEP (salvaged) — on-chain execution
    tx.py                # Besu build/send/nonce/gas/revert (extracted from primedelta.py)
    dex/ handlers.py params.py     # kept
    quoting.py           # NEW — V3 QuoterV2 + slot0 spot price
  contracts.py           # kept
  networks/              # kept loader; refresh dev.json, add testnet.json (+mainnet.json last)
  models/                # types.py split into cohesive modules
  settings.py            # kept
```

## Definition of Done
A `primedelta` Python package that:
1. **Works end-to-end** against dev (and, once configured, testnet + mainnet): login →
   portfolio → order → deposit/withdraw → swap → LP, all green on live infra.
2. **Is published** to the agreed index (public PyPI *or* private — gated on the license
   decision) with versioning + an automated release pipeline.
3. **Is user/developer-friendly:** a `Signer` abstraction (raw key / keystore / mnemonic / KMS /
   MetaMask-via-bridge), type hints delivered (`py.typed`), quoting/preview helpers, refreshed
   README + examples, CHANGELOG, and CI enforcing format/lint/type/tests.
4. **Won't silently rot again:** a live-contract-drift test that fails when the backend auth
   contract / endpoints / ABIs move under it.

## Guiding principles
- **Reality gate first.** Nothing is trusted until it runs against live dev. Phase 0 proves the
  core works before we build on it.
- **Signer seam is the backbone** — land it early; everything wallet-related is then additive.
- **Backward compatible** — `private_key=` keeps working throughout.
- **No secrets in the repo.** Dev keys are public/deterministic but NEVER onto testnet/mainnet.
- Each phase ends **green on CI + validated on dev**, its own PR, cold-reviewed.

---

## Phase 0 — Validate & un-break the core  *(reality gate — do first)*
Goal: `login()` + one authed read + one swap actually succeed against **dev**.
- [ ] 0.1 Stand up a throwaway venv, install the pinned deps, run the unit suite as a baseline.
- [ ] 0.2 Reproduce the breakage live on dev: attempt `login()` → confirm the 204/no-token +
      cookie/CSRF failure; capture exact backend behavior.
- [ ] 0.3 **Rewrite `PrimeDeltaClient` onto a `requests.Session`** (persistent cookie jar):
      `/users/verify/` sets `dclex_auth`; fetch `X-CSRFToken` via `GET /csrf-token/` and attach
      on unsafe methods; drop the `Authorization: Token/Bearer` + body-token model. Add `me()`.
- [ ] 0.4 Fix signed prices: `/signed-prices/` over the cookie session (requires VERIFIED);
      remove the Bearer header.
- [ ] 0.5 Fix price stream: mint `GET /prices-stream-token/` then open `/prices-stream/?token=`.
- [ ] 0.6 Migrate stablecoin routes to dUSD: `/initialize-stablecoin-withdraw/` +
      `/deposit-stablecoin-signature/` (keep legacy behind a flag if `DUSD_ENABLED` varies by env).
- [ ] 0.7 **Verify/fix the web3-v7 `rawTransaction` → `raw_transaction`** attribute; run one
      local-key tx on dev to prove submission works.
- [ ] 0.8 **Refresh `networks/dev.json`** addresses + ABIs from the current dev deployment
      (Router `allStockTokens`, factory, vault, DID, NPM, oracle, WDEL, stablecoin) and add a
      small script to regenerate config from a deployment (used again in Phase 5).
- [ ] 0.9 **Green light:** scripted end-to-end on dev — login → `portfolio()` → `stocks()` →
      a tiny `swap_exact_input` → on-chain balance delta. Document the run.

## Phase 1 — `Signer` abstraction  *(backbone; behavior-preserving)*
- [ ] 1.1 New `signer.py`: `Signer` Protocol (`address`, `sign_message`, `submit_transaction`,
      `fills_gas_and_nonce`).
- [ ] 1.2 `LocalAccountSigner` wrapping today's `LocalAccount`; `from_key` classmethod.
- [ ] 1.3 Route `PrimeDelta.__init__` (`signer=` optional, `private_key=` → thin wrapper),
      `login()` (one line), and `_build_and_send_transaction_once` (branch on
      `fills_gas_and_nonce`) through `self._signer`. Handlers already only read `.address`.
- [ ] 1.4 Existing unit tests must pass unchanged; add tests for the wrapper + dispatch.

## Phase 2 — Headless provisioning  *(kill the plaintext-key habit)*
- [ ] 2.1 `LocalAccountSigner.from_keystore(path, password)` (eth-account `decrypt`).
- [ ] 2.2 `LocalAccountSigner.from_mnemonic(phrase, index)` (HD derivation).
- [ ] 2.3 Documented `from_env` / secret-manager recipe (AWS SM / GCP SM / Vault KV).
- [ ] 2.4 `KmsSigner` (AWS KMS secp256k1, key never leaves HSM: DER decode → low-s → recover `v`);
      ship `boto3` as an optional extra `[kms]`. (Optional `VaultSigner`, same shape.)
- [ ] 2.5 Tests: keystore/mnemonic round-trip; KMS signer against a mocked KMS client.

## Phase 3 — Interactive MetaMask bridge  *(the "plug in MetaMask" ask)*
- [ ] 3.1 `BrowserSigner`: stdlib loopback HTTP server (127.0.0.1:0, one-time `state`, single-use,
      immediate shutdown) + a self-contained signing page using EIP-6963 provider discovery →
      `personal_sign` (SIWE) and `eth_sendTransaction` (wallet fills nonce/gas + broadcasts).
- [ ] 3.2 `wallet_switchEthereumChain` / `addEthereumChain` from bundled `networks/*.json`.
- [ ] 3.3 `MockBrowserSigner` (same contract, auto-signs with a dev key) + `ImpersonatingSigner`
      (anvil/hardhat impersonation) so the `fills_gas_and_nonce=True` path is CI-covered.
- [ ] 3.4 Security review of the bridge (loopback-only, state token, no wildcard CORS, no logging
      of sig/calldata, hard timeout). Example script.

## Phase 4 — Feature completeness  *(fill the surface gaps, prioritized)*
Backend wrappers:
- [ ] 4.1 **Messages** — `messages()` + `mark_message_read(id)` (`GET /messages/`, `POST /messages/{id}/`).
- [ ] 4.2 **dUSD** — `deposit_stablecoin` signature path + `request_stablecoin_withdrawal` on dUSD route.
- [ ] 4.3 **Fiat** — `bank_details()` (deposit instructions) + `request_fiat_withdrawal(...)`.
- [ ] 4.4 **Cost/quote** — `limit_buy_cost` / `limit_sell_cost` / `market_sell_cost`.
- [ ] 4.5 **Helpers** — `swappable_symbols()`, `application_settings()`, `portfolio_history(range)`,
      `digital_identity_id()`.
On-chain:
- [ ] 4.6 **Quoting** — bundle Uniswap V3 **QuoterV2** ABI+address; `quote_swap(...)` for AMM;
      expose `spot_price(symbol)` via `univ3_pool.slot0()`. Decide DclexPool quote path
      (contract view vs oracle-price+fee-curve; needs an oracle price-read ABI — flag upstream).
- [ ] 4.7 **Allowances** — `allowance(token, spender)`, `approve(token, spender, amount)`,
      `revoke_approval(token, spender)`.
- [ ] 4.8 **DID reads** — `did_token_id()`, `is_pro()`, `is_valid()` (recovery-state getter is
      absent from the ABI → note as upstream-blocked).
- [ ] 4.9 **V3 lifecycle** — `increase_liquidity(position_id, ...)`, `burn_position(position_id)`;
      accurate LP fee preview via static-`collect` sim.
- [ ] 4.10 **Native DEL** — `send_del(to, amount)`.

## Phase 5 — Multi-network
- [ ] 5.1 `networks/testnet.json` (chain 7357) + `networks/mainnet.json` (chain 4109) via the
      Phase-0 config generator; verify addresses on-chain.
- [ ] 5.2 Chain-switch story: `PrimeDelta(..., network="testnet"|"mainnet")` validated
      end-to-end per env (mainnet writes only on explicit user go).
- [ ] 5.3 Document the "refresh config after a redeploy" runbook.

## Phase 6 — Test hardening  *(so it can't silently rot)*
- [ ] 6.1 Unit-test the rewritten `PrimeDeltaClient`: mock `requests.Session`, assert cookie +
      CSRF handling, request building, camelCase→dataclass parsing, error mapping (400/401/403/204).
- [ ] 6.2 Integration tests that hit **live dev** for the full happy path (login→portfolio→order→
      deposit/withdraw→swap→LP), self-skipping without creds; make the bootstrap env-aware
      (not anvil-only).
- [ ] 6.3 **Live-contract-drift test** — asserts the backend still honors the SDK's auth contract
      + key endpoint shapes + bundled ABIs match on-chain selectors; this is the test that would
      have caught the March→now drift. Runs in CI against dev on a schedule.
- [ ] 6.4 Coverage gate (pytest-cov), target ≥ ~85% on non-integration paths.

## Phase 7 — Packaging, CI, docs, publish  *(ship it)*
- [ ] 7.1 Add `src/primedelta/py.typed` + include in package-data (deliver type hints).
- [ ] 7.2 Loosen runtime deps to compatible ranges; unify dev deps into one source; add
      `[tool.black]/[tool.isort]/[tool.mypy]` + `[tool.coverage]` config.
- [ ] 7.3 **Resolve LICENSE vs publish target** (see Open Decisions) — set license + trove
      classifier accordingly.
- [ ] 7.4 Metadata: `[project.urls]` (Homepage/Repo/Docs), authors/maintainers, keywords,
      richer classifiers; README rework (absolute links so PyPI renders), CHANGELOG, CONTRIBUTING.
- [ ] 7.5 **GitHub Actions CI**: matrix (3.10–3.12) running black --check, isort --check, mypy,
      pytest (unit) + a gated integration/drift job.
- [ ] 7.6 **Release pipeline**: tag → build (`python -m build`) → publish to the chosen index
      (public PyPI via OIDC Trusted Publishing, or private CodeArtifact/Gemfury). Version scheme
      (static bump or `setuptools_scm`).
- [ ] 7.7 Refresh `examples/` to the new auth + Signer API; add a MetaMask-bridge example.
- [ ] 7.8 **DoD sign-off:** install the published package fresh in a clean env and run the quick
      start against dev.

---

## Wallet-connection design (condensed)

The whole wallet dependency is 3 operations: **`address`**, **`sign_message`** (SIWE), and
**submit a transaction**. Model a `Signer` Protocol with those + a `fills_gas_and_nonce` flag
that distinguishes local signers (SDK fills nonce/gas, signer signs+broadcasts a raw tx) from
wallet signers (MetaMask/WalletConnect fill nonce/gas and broadcast themselves).

Implementations, by axis of the PO's two asks:
- **"provision private keys"** → `LocalAccountSigner` (key | keystore | mnemonic) now +
  `KmsSigner` (key never leaves the HSM — production posture, fits infra already running KMS
  signers for the chain).
- **"plug in MetaMask"** → `BrowserSigner`: a local **loopback HTTP bridge** + a tiny
  `window.ethereum` page (`personal_sign` + `eth_sendTransaction`). Stdlib-only, no external
  services, per-tx confirmation in the user's own extension. **WalletConnect v2 is explicitly
  parked** — no maintained Python v2 library; the bridge covers the same intent.
- **CI/dev** → `MockBrowserSigner` + anvil `ImpersonatingSigner` keep the interactive path
  covered without a human.

`PrimeDelta(...)` gains `signer=`; `private_key=` becomes a thin `LocalAccountSigner.from_key`
wrapper. Refactor footprint ≈ 3 lines in `login()`/`__init__` + one branch in the tx builder +
the new `signer.py`. No handler edits.

Rollout order: **Phase 1** (protocol + LocalAccountSigner) → **Phase 2** (keystore/mnemonic/KMS)
→ **Phase 3** (browser bridge) → deferred (Ledger, WalletConnect).

---

## Decisions
1. ✅ **Direction:** keep good modules + rewrite rest + restructure (see header + §Restructure).
2. ✅ **Publish:** public PyPI + relicense. → Remaining sub-decision: exact license
   (**Apache-2.0** proposed) — PO/legal to confirm before publish; does not block building.
3. ✅ **Networks:** dev → testnet now; mainnet last.
4. ⬜ **v1.0 scope (open, non-blocking):** propose first published release = Phases 0–3 + slim
   Phase 4 (messages, dUSD, quoting), then iterate on the rest. Confirm when we reach Phase 7.

## Risks / watch-items
- Dev config drift (addresses change every redeploy) — the config generator + drift test mitigate.
- KMS `v`-recovery + low-s normalization is fiddly — cover with a mocked-KMS test vs a known key.
- Browser bridge is desktop/interactive-only — never the CI path; keep raw-key/mock for automation.
- DclexPool quote + DID recovery-state both need **upstream ABI additions** (oracle price getter;
  DID recovery getter) — track as dependencies, don't block the rest.
- Mainnet: writes only on explicit user go; no synthetic supply; validate read paths first.
