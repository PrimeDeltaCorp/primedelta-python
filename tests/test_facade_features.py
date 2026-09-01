from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from primedelta import NotEnoughFunds, PrimeDelta
from primedelta.primedelta_client import APIError
from primedelta.types import OrderSide


def _pd():
    with patch("primedelta.primedelta.Web3"):
        pd = PrimeDelta(private_key="0x" + "1" * 64, web3_provider_url="http://x")
    pd._primedelta_client = MagicMock()
    return pd


class TestFacadeFeatures:
    def test_request_fiat_withdrawal_maps_insufficient_funds(self):
        pd = _pd()
        pd._primedelta_client.request_fiat_withdrawal.side_effect = APIError(
            "INSUFFICIENT_FUNDS"
        )
        with pytest.raises(NotEnoughFunds):
            pd.request_fiat_withdrawal(Decimal("1"))

    def test_request_fiat_withdrawal_reraises_other_api_errors(self):
        pd = _pd()
        pd._primedelta_client.request_fiat_withdrawal.side_effect = APIError("OTHER")
        with pytest.raises(APIError):
            pd.request_fiat_withdrawal(Decimal("1"))

    def test_limit_buy_cost_delegates_with_buy_side(self):
        pd = _pd()
        pd.limit_buy_cost("AAPL", 1, Decimal("100"))
        args = pd._primedelta_client.limit_order_cost.call_args.args
        assert args[0] == OrderSide.BUY
        assert args[1] == "AAPL"

    def test_limit_sell_cost_delegates_with_sell_side(self):
        pd = _pd()
        pd.limit_sell_cost("AAPL", 1, Decimal("100"))
        assert (
            pd._primedelta_client.limit_order_cost.call_args.args[0] == OrderSide.SELL
        )

    def test_digital_identity_id_delegates(self):
        pd = _pd()
        pd._primedelta_client.digital_identity_id.return_value = 47
        assert pd.digital_identity_id() == 47


class TestAllowancesAndSend:
    def _pd(self):
        pd = _pd()
        pd._signer = MagicMock()
        pd._signer.address = "0xME"
        pd._web3 = MagicMock()
        pd._web3.to_checksum_address.side_effect = lambda a: a
        pd._build_and_send_transaction = MagicMock(return_value="0xTX")
        pd._build_and_send_value_transaction = MagicMock(return_value="0xVAL")
        pd._token_ref = MagicMock(return_value=("0xTOKEN", 6))
        erc20 = MagicMock()
        pd._erc20 = MagicMock(return_value=erc20)
        return pd, erc20

    def test_allowance_scales_by_decimals(self):
        pd, erc20 = self._pd()
        erc20.functions.allowance.return_value.call.return_value = 5_000_000
        assert pd.allowance("dUSD", "0xSPENDER") == Decimal("5")

    def test_approve_scales_and_sends(self):
        pd, erc20 = self._pd()
        assert pd.approve("dUSD", "0xSPENDER", Decimal("5")) == "0xTX"
        erc20.functions.approve.assert_called_once_with("0xSPENDER", 5_000_000)

    def test_revoke_approval_sends_zero(self):
        pd, erc20 = self._pd()
        pd.revoke_approval("dUSD", "0xSPENDER")
        erc20.functions.approve.assert_called_once_with("0xSPENDER", 0)

    def test_send_del_scales_by_1e18(self):
        pd, _ = self._pd()
        assert pd.send_del("0xTO", Decimal("0.001")) == "0xVAL"
        pd._build_and_send_value_transaction.assert_called_once_with("0xTO", 10**15)

    def test_value_transaction_local_fills_gas_and_nonce(self):
        pd = _pd()
        pd._signer = MagicMock()
        pd._signer.address = "0xME"
        pd._signer.fills_gas_and_nonce = False
        sent = MagicMock()
        sent.hex.return_value = "0xH"
        pd._signer.submit_transaction.return_value = sent
        pd._web3 = MagicMock()
        pd._web3.to_checksum_address.side_effect = lambda a: a
        pd._web3.eth.gas_price = 10**9
        pd._web3.eth.get_transaction_count.return_value = 3
        pd._web3.eth.wait_for_transaction_receipt.return_value = {"status": 1}

        assert pd._build_and_send_value_transaction("0xTO", 10**15) == "0xH"
        tx = pd._signer.submit_transaction.call_args.args[1]
        assert tx["to"] == "0xTO"
        assert tx["value"] == 10**15
        assert tx["gas"] == 21_000
        assert tx["nonce"] == 3
        assert tx["gasPrice"] == 10**9

    def test_value_transaction_wallet_omits_gas_and_nonce(self):
        pd = _pd()
        pd._signer = MagicMock()
        pd._signer.address = "0xME"
        pd._signer.fills_gas_and_nonce = True
        sent = MagicMock()
        sent.hex.return_value = "0xH"
        pd._signer.submit_transaction.return_value = sent
        pd._web3 = MagicMock()
        pd._web3.to_checksum_address.side_effect = lambda a: a
        pd._web3.eth.wait_for_transaction_receipt.return_value = {"status": 1}

        pd._build_and_send_value_transaction("0xTO", 5)
        tx = pd._signer.submit_transaction.call_args.args[1]
        assert "gas" not in tx and "nonce" not in tx and "gasPrice" not in tx

    def test_value_transaction_retries_on_nonce_too_low(self):
        from primedelta.primedelta import TransactionFailed

        pd = _pd()
        pd._signer = MagicMock()
        pd._signer.address = "0xME"
        calls = {"n": 0}

        def once(to, value):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransactionFailed(
                    "transfer", "nonce too low; expected account nonce 7"
                )
            return "0xOK"

        with patch("time.sleep"):
            pd._build_and_send_value_transaction_once = MagicMock(side_effect=once)
            assert pd._build_and_send_value_transaction("0xTO", 5) == "0xOK"
        assert calls["n"] == 2
        assert pd._next_nonce == 6


class TestDidReads:
    def _pd(self, token_id, is_pro=True, is_valid=True):
        pd = _pd()
        pd._signer = MagicMock()
        pd._signer.address = "0xME"
        did = MagicMock()
        pd._did_contract = MagicMock(return_value=did)
        did.functions.getId.return_value.call.return_value = token_id
        did.functions.isPro.return_value.call.return_value = is_pro
        did.functions.isValid.return_value.call.return_value = is_valid
        return pd, did

    def test_did_token_id(self):
        pd, _ = self._pd(47)
        assert pd.did_token_id() == 47

    def test_did_token_id_none_when_zero(self):
        pd, _ = self._pd(0)
        assert pd.did_token_id() is None

    def test_is_pro_reads_token(self):
        pd, did = self._pd(47, is_pro=True)
        assert pd.is_pro() is True
        did.functions.isPro.assert_called_once_with(47)

    def test_is_pro_false_without_did(self):
        pd, did = self._pd(0)
        assert pd.is_pro() is False
        did.functions.isPro.assert_not_called()

    def test_is_valid_reads_token_and_false_without_did(self):
        pd, did = self._pd(47, is_valid=True)
        assert pd.is_valid() is True
        did.functions.isValid.assert_called_once_with(47)
        pd_no, did_no = self._pd(0)
        assert pd_no.is_valid() is False
        did_no.functions.isValid.assert_not_called()


class TestAgentSafetyRails:
    def test_min_out_from_quote(self):
        assert PrimeDelta.min_out_from_quote(Decimal("100"), 100) == Decimal("99")
        assert PrimeDelta.min_out_from_quote(Decimal("100"), 0) == Decimal("100")
        assert PrimeDelta.min_out_from_quote(Decimal("50"), 250) == Decimal("48.75")

    def test_min_out_from_quote_rejects_bad_slippage(self):
        with pytest.raises(ValueError):
            PrimeDelta.min_out_from_quote(Decimal("100"), -1)
        with pytest.raises(ValueError):
            PrimeDelta.min_out_from_quote(Decimal("100"), 10_001)

    def test_decode_revert_recognizes_stale_price(self):
        from web3.exceptions import ContractLogicError

        from primedelta.primedelta import _decode_revert

        reason = _decode_revert(ContractLogicError("reverted", data="0x19abf40e"))
        assert "StalePrice" in reason

    def _tx_pd(self):
        pd = _pd()
        pd._signer = MagicMock()
        pd._signer.address = "0xME"
        pd._signer.fills_gas_and_nonce = False
        pd._web3 = MagicMock()
        pd._web3.to_checksum_address.side_effect = lambda a: a
        pd._web3.eth.gas_price = 10**9
        pd._web3.eth.get_transaction_count.return_value = 1
        pd._get_contracts = MagicMock(return_value=MagicMock(chain_id=2028))
        pd._try_debug_trace_call = MagicMock(return_value=None)
        return pd

    def _fn_reverting_with(self, data):
        from web3.exceptions import ContractLogicError

        fn = MagicMock()
        fn.fn_name = "swap"
        fn.address = "0xPOOL"
        fn._encode_transaction_data.return_value = "0xdead"
        fn.build_transaction.side_effect = ContractLogicError("reverted", data=data)
        return fn

    def test_stale_price_revert_raises_market_closed(self):
        from primedelta import MarketClosed, TransactionFailed

        pd = self._tx_pd()
        with pytest.raises(MarketClosed) as info:
            pd._build_and_send_transaction_once(self._fn_reverting_with("0x19abf40e"))
        assert isinstance(info.value, TransactionFailed)  # still a TransactionFailed

    def test_other_revert_stays_transaction_failed(self):
        from primedelta import MarketClosed, TransactionFailed

        pd = self._tx_pd()
        with pytest.raises(TransactionFailed) as info:
            # a generic panic selector must NOT be classified as MarketClosed
            pd._build_and_send_transaction_once(self._fn_reverting_with("0x4e487b71"))
        assert not isinstance(info.value, MarketClosed)

    def test_halt_blocks_sends_resume_reenables(self):
        from primedelta import TradingHalted

        pd = _pd()
        pd._halted = False
        pd._tx_lock = __import__("threading").Lock()
        pd.halt()
        assert pd.is_halted is True
        with pytest.raises(TradingHalted):
            pd._send_with_nonce_retry(lambda: "0xTX")
        pd.resume()
        assert pd.is_halted is False
        assert pd._send_with_nonce_retry(lambda: "0xTX") == "0xTX"

    def test_instrument_kind_amm_vs_oracle(self):
        from primedelta.dex.handlers import PoolNotFound

        pd = _pd()
        pd._web3 = MagicMock()
        pd._get_contracts = MagicMock(return_value=MagicMock())
        with patch(
            "primedelta.dex.handlers._resolve_stock_token", return_value="0xSTK"
        ):
            with patch(
                "primedelta.dex.handlers._lookup_amm_pool_address",
                return_value="0xPOOL",
            ):
                assert pd.instrument_kind("AMMT1") == "amm"
            with patch(
                "primedelta.dex.handlers._lookup_amm_pool_address",
                side_effect=PoolNotFound("no amm"),
            ):
                assert pd.instrument_kind("AAPL") == "oracle"


class TestChainIdCache:
    """`_install_chain_id_cache` caches the immutable eth_chainId at the provider
    (one round-trip instead of one per call), passes other methods straight
    through, and never caches an error response."""

    def _wrap(self, responses):
        from primedelta.primedelta import _install_chain_id_cache

        calls = []

        class _Provider:
            def make_request(self, method, params):
                calls.append(method)
                return responses(method, len(calls))

        class _Web3:
            def __init__(self) -> None:
                self.provider = _Provider()

        w3 = _Web3()
        _install_chain_id_cache(w3)
        return w3, calls

    def test_caches_chain_id_and_passes_other_methods_through(self):
        def responses(method, n):
            if method == "eth_chainId":
                return {"jsonrpc": "2.0", "id": n, "result": "0x7ec"}
            return {"jsonrpc": "2.0", "id": n, "result": "0x1"}

        w3, calls = self._wrap(responses)
        r1 = w3.provider.make_request("eth_chainId", [])
        r2 = w3.provider.make_request("eth_chainId", [])
        assert r1["result"] == r2["result"] == "0x7ec"
        assert calls.count("eth_chainId") == 1  # second served from cache
        w3.provider.make_request("eth_blockNumber", [])
        w3.provider.make_request("eth_blockNumber", [])
        assert calls.count("eth_blockNumber") == 2  # never cached

    def test_does_not_cache_an_error_response(self):
        seq = [
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "boom"}},
            {"jsonrpc": "2.0", "id": 2, "result": "0x7ec"},
        ]

        def responses(method, n):
            return seq[n - 1]

        w3, calls = self._wrap(responses)
        first = w3.provider.make_request("eth_chainId", [])
        assert "error" in first  # not cached
        second = w3.provider.make_request("eth_chainId", [])
        assert second["result"] == "0x7ec"  # re-requested, now succeeds + caches
        assert calls.count("eth_chainId") == 2
