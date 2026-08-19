"""Live-contract-drift guard — the test that would have caught the March→now rot.

Runs against live dev (self-skips without creds). Asserts three contracts the
SDK silently depends on still hold:

1. Auth contract — SIWE login + cookie/CSRF still yields an authenticated
   session (`me()` returns the wallet, `logged_in()` is True).
2. Endpoint shapes — the key reads still parse into the SDK's dataclasses
   (a renamed/removed backend field surfaces here as a KeyError, not in prod).
3. Bundled ABIs vs on-chain — every function the SDK actually calls is present
   as a 4-byte selector in the deployed bytecode. solc emits each external
   selector as a PUSH4 in the dispatcher, so selector-in-code is a reliable
   drift signal for these (non-proxy) contracts. A changed signature moves the
   selector and trips this test. Scope: the fixed core contracts only — the
   per-symbol pool / UniV3-factory ABIs live at addresses discovered at runtime,
   so their drift is out of scope here (and `vault` has no on-chain SDK call).

Run: pytest tests/integration/test_contract_drift.py -v -m integration
(CI runs this on a schedule against dev.)
"""
import pytest
from eth_utils import function_abi_to_4byte_selector

pytestmark = pytest.mark.integration


# Only the functions the SDK genuinely calls — a curated set, verified present
# in dev bytecode, so this stays a true drift signal rather than a proxy false
# positive.
_SDK_CALLS = {
    "stablecoin": ["symbol", "decimals", "balanceOf", "transfer", "approve", "allowance"],
    "factory": [
        "mintStocks",
        "burnStocks",
        "mintStablecoin",
        "burnStablecoin",
        "getStocksCount",
        "getNonce",
    ],
    "digital_identity": ["getId", "isPro", "isValid", "mint"],
    "dex_router": [
        "buyExactInput",
        "sellExactInput",
        "buyExactOutput",
        "sellExactOutput",
        "swapExactInput",
        "swapExactOutput",
        "stockTokenToPool",
        "allStockTokens",
    ],
    "oracle": ["getUpdateFee"],
    "position_manager": [
        "positions",
        "mint",
        "increaseLiquidity",
        "decreaseLiquidity",
        "collect",
        "burn",
        "multicall",
        "factory",
        "balanceOf",
        "tokenOfOwnerByIndex",
    ],
    "quoter": ["quoteExactInputSingle", "quoteExactOutputSingle"],
    "wdel": ["deposit", "withdraw"],
}


class TestContractDrift:
    def test_auth_contract_holds(self, primedelta):
        primedelta.login()
        try:
            assert primedelta.logged_in() is True
            me = primedelta._primedelta_client.me()
            assert me.lower() == primedelta._signer.address.lower()
        finally:
            primedelta.logout()

    def test_endpoint_shapes_parse(self, primedelta_logged_in):
        pd = primedelta_logged_in
        # Each of these parses a camelCase payload into a dataclass; a drifted
        # field name raises inside the SDK parser rather than here.
        portfolio = pd.portfolio()
        assert portfolio is not None
        stocks = pd.stocks()
        assert isinstance(stocks, dict) and len(stocks) > 0
        settings = pd.application_settings()
        assert settings is not None

    def test_bundled_abis_match_onchain_selectors(self, primedelta):
        w3 = primedelta._web3
        core = primedelta._get_contracts().core
        drift = []
        for name, fns in _SDK_CALLS.items():
            ref = getattr(core, name)
            assert ref is not None, f"{name} missing from network config"
            code = w3.eth.get_code(w3.to_checksum_address(ref.address)).hex()
            assert len(code) > 2, f"{name} @ {ref.address} has no bytecode"
            by_name = {
                a.get("name"): a for a in ref.abi if a.get("type") == "function"
            }
            for fn in fns:
                abi = by_name.get(fn)
                if abi is None:
                    drift.append(f"{name}.{fn}: absent from bundled ABI")
                    continue
                selector = function_abi_to_4byte_selector(abi).hex()
                if selector not in code:
                    drift.append(
                        f"{name}.{fn}: selector 0x{selector} not in deployed code"
                    )
        assert not drift, "ABI drift vs on-chain:\n" + "\n".join(drift)
