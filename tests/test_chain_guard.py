from decimal import Decimal
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from primedelta import NetworkMismatch, PrimeDelta

_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def _pd(network, chain_id):
    with patch("primedelta.primedelta.Web3"):
        pd = PrimeDelta(
            private_key="0x" + "1" * 64, web3_provider_url="http://x", network=network
        )
    pd._web3 = MagicMock()
    pd._web3.to_checksum_address.side_effect = lambda a: a
    reads = PropertyMock(
        side_effect=chain_id if isinstance(chain_id, list) else None,
        return_value=chain_id,
    )
    type(pd._web3.eth).chain_id = reads
    pd._primedelta_client = MagicMock()
    pd._primedelta_client.get_nonce.return_value = "nonce12345"
    pd._signer = MagicMock()
    pd._signer.address = _ADDR
    pd._signer.sign_message.return_value = "0xsig"
    return pd, reads


def _contract_call():
    fn = MagicMock()
    fn.fn_name = "approve"
    fn.address = "0xC0FFEE"
    fn._encode_transaction_data.return_value = "0xda7a"
    return fn


class TestChainGuard:
    def test_login_refuses_an_rpc_on_another_chain(self):
        pd, _ = _pd("testnet", 2028)
        with pytest.raises(NetworkMismatch) as exc:
            pd.login()
        assert str(exc.value) == (
            "the RPC is chain 2028, but network='testnet' expects chain 7357 — "
            "pass the network that matches your RPC, or fix web3_provider_url."
        )
        pd._primedelta_client.get_nonce.assert_not_called()
        pd._signer.sign_message.assert_not_called()

    def test_a_send_refuses_an_rpc_on_another_chain(self):
        pd, _ = _pd("testnet", 2028)
        with pytest.raises(NetworkMismatch):
            pd._build_and_send_transaction(_contract_call())
        with pytest.raises(NetworkMismatch):
            pd.send_del(_ADDR, Decimal("1"))
        pd._signer.submit_transaction.assert_not_called()

    def test_craft_refuses_an_rpc_on_another_chain(self):
        pd, _ = _pd("testnet", 2028)
        action = MagicMock()
        with pytest.raises(NetworkMismatch):
            pd.craft(action)
        action.assert_not_called()

    def test_dev_refuses_the_testnet_chain(self):
        pd, _ = _pd("dev", 7357)
        with pytest.raises(NetworkMismatch, match="network='dev' expects chain 2028"):
            pd.login()

    def test_a_matching_chain_is_read_once(self):
        pd, reads = _pd("testnet", 7357)
        pd.login()
        pd.login()
        assert pd.craft(lambda: pd._build_and_send_transaction(_contract_call()))
        assert reads.call_count == 1
        pd._primedelta_client.login.assert_called()

    def test_dev_accepts_the_local_anvil_chain(self):
        pd, _ = _pd("dev", 31337)
        pd.login()
        pd._primedelta_client.login.assert_called_once()

    def test_testnet_refuses_the_local_anvil_chain(self):
        pd, _ = _pd("testnet", 31337)
        with pytest.raises(NetworkMismatch, match="the RPC is chain 31337"):
            pd.login()

    def test_an_unreachable_rpc_is_checked_again_on_the_next_call(self):
        pd, reads = _pd("testnet", [ConnectionError("down"), 2028])
        pd.login()
        with pytest.raises(NetworkMismatch):
            pd.login()
        assert reads.call_count == 2
