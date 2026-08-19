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
