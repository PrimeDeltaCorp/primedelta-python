from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from primedelta import PrimeDelta
from primedelta.primedelta import TransactionFailed


def _pd():
    with patch("primedelta.primedelta.Web3"):
        pd = PrimeDelta(private_key="0x" + "1" * 64, web3_provider_url="http://x")
    pd._signer = MagicMock()
    pd._signer.address = "0xME"
    pd._signer.fills_gas_and_nonce = False  # crafting must work even for a local key
    pd._web3 = MagicMock()
    pd._web3.to_checksum_address.side_effect = lambda a: a
    return pd


class TestCraft:
    def test_crafts_value_transfer_without_broadcasting(self):
        pd = _pd()
        chain_id = pd._get_contracts().chain_id

        txs = pd.craft(lambda: pd.send_del("0xTO", Decimal("0.001")))

        assert txs == [
            {
                "from": "0xME",
                "to": "0xTO",
                "value": 10**15,
                "data": "0x",
                "chainId": chain_id,
            }
        ]
        # nothing was signed or sent
        pd._signer.submit_transaction.assert_not_called()

    def test_crafts_contract_call_calldata(self):
        pd = _pd()
        erc20 = MagicMock()
        pd._erc20 = MagicMock(return_value=erc20)
        pd._token_ref = MagicMock(return_value=("0xTOKEN", 6))
        fn = erc20.functions.approve.return_value
        fn.address = "0xTOKEN"
        fn.fn_name = "approve"
        fn._encode_transaction_data.return_value = "0xAPPROVE"

        txs = pd.craft(lambda: pd.approve("dUSD", "0xSPENDER", Decimal("5")))

        assert txs == [
            {
                "from": "0xME",
                "to": "0xTOKEN",
                "value": 0,
                "data": "0xAPPROVE",
                "chainId": pd._get_contracts().chain_id,
            }
        ]
        erc20.functions.approve.assert_called_once_with("0xSPENDER", 5_000_000)
        pd._signer.submit_transaction.assert_not_called()

    def test_multi_step_action_captures_each_tx_in_order(self):
        pd = _pd()
        erc20 = MagicMock()
        pd._erc20 = MagicMock(return_value=erc20)
        pd._token_ref = MagicMock(return_value=("0xTOKEN", 6))
        fn = erc20.functions.approve.return_value
        fn.address = "0xTOKEN"
        fn.fn_name = "approve"
        fn._encode_transaction_data.return_value = "0xAPPROVE"

        def two_approvals():
            pd.approve("dUSD", "0xA", Decimal("5"))
            pd.approve("dUSD", "0xB", Decimal("7"))

        txs = pd.craft(two_approvals)

        assert len(txs) == 2
        assert erc20.functions.approve.call_args_list[0].args == ("0xA", 5_000_000)
        assert erc20.functions.approve.call_args_list[1].args == ("0xB", 7_000_000)

    def test_raises_when_calldata_cannot_be_encoded(self):
        pd = _pd()
        erc20 = MagicMock()
        pd._erc20 = MagicMock(return_value=erc20)
        pd._token_ref = MagicMock(return_value=("0xTOKEN", 6))
        fn = erc20.functions.approve.return_value
        fn._encode_transaction_data.side_effect = ValueError("bad abi")

        with pytest.raises(TransactionFailed):
            pd.craft(lambda: pd.approve("dUSD", "0xSPENDER", Decimal("5")))

    def test_crafting_flag_reset_after_action(self):
        pd = _pd()
        pd.craft(lambda: pd.send_del("0xTO", Decimal("0.001")))
        assert pd._crafting is None

    def test_crafting_flag_reset_after_exception(self):
        pd = _pd()

        def boom():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            pd.craft(boom)
        assert pd._crafting is None

    def test_craft_cannot_be_nested(self):
        pd = _pd()
        with pytest.raises(RuntimeError, match="nested"):
            pd.craft(lambda: pd.craft(lambda: None))

    def test_as_unsigned_normalises_value_and_data(self):
        assert PrimeDelta._as_unsigned(
            {"from": "0xA", "to": "0xB", "chainId": 2028}
        ) == {"from": "0xA", "to": "0xB", "value": 0, "data": "0x", "chainId": 2028}
