import json
from unittest.mock import MagicMock, patch

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from primedelta import PrimeDelta
from primedelta.signer import (
    KmsSigner,
    LocalAccountSigner,
    Signer,
    _SECP256K1_N,
    _SPKI_PREFIX,
)

KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
ANVIL_MNEMONIC = "test test test test test test test test test test test junk"


class TestLocalAccountSigner:
    def test_from_key_exposes_address_and_flag(self):
        signer = LocalAccountSigner.from_key(KEY)
        assert signer.address == ADDR
        assert signer.fills_gas_and_nonce is False

    def test_satisfies_signer_protocol(self):
        assert isinstance(LocalAccountSigner.from_key(KEY), Signer)

    def test_sign_message_recovers_to_address(self):
        signer = LocalAccountSigner.from_key(KEY)
        signature = signer.sign_message("hello")
        recovered = Account.recover_message(
            encode_defunct(text="hello"),
            signature=bytes.fromhex(signature.removeprefix("0x")),
        )
        assert recovered == ADDR

    def test_submit_transaction_signs_and_broadcasts(self):
        signer = LocalAccountSigner.from_key(KEY)
        web3 = MagicMock()
        web3.eth.send_raw_transaction.return_value = b"\xaa"
        tx = {
            "to": ADDR,
            "value": 1,
            "gas": 21000,
            "gasPrice": 10**9,
            "nonce": 0,
            "chainId": 2028,
        }
        assert signer.submit_transaction(web3, tx) == b"\xaa"
        sent = web3.eth.send_raw_transaction.call_args.args[0]
        assert isinstance(sent, (bytes, bytearray)) and len(sent) > 0

    def test_from_keystore_round_trip(self, tmp_path):
        path = tmp_path / "keystore.json"
        path.write_text(json.dumps(Account.encrypt(KEY, "pw123")))
        assert LocalAccountSigner.from_keystore(path, "pw123").address == ADDR

    def test_from_keystore_wrong_password_raises(self, tmp_path):
        path = tmp_path / "keystore.json"
        path.write_text(json.dumps(Account.encrypt(KEY, "pw123")))
        with pytest.raises(ValueError):
            LocalAccountSigner.from_keystore(path, "wrong")

    def test_from_mnemonic_derives_expected_index(self):
        assert LocalAccountSigner.from_mnemonic(ANVIL_MNEMONIC, index=1).address == ADDR

    def test_from_mnemonic_index_selects_account(self):
        a0 = LocalAccountSigner.from_mnemonic(ANVIL_MNEMONIC, index=0)
        a1 = LocalAccountSigner.from_mnemonic(ANVIL_MNEMONIC, index=1)
        assert a0.address != a1.address
        assert a1.address == ADDR


class TestPrimeDeltaSignerWiring:
    def _pd(self, **kwargs):
        with patch("primedelta.primedelta.Web3"):
            return PrimeDelta(web3_provider_url="http://localhost:8545", **kwargs)

    def test_private_key_builds_local_signer(self):
        pd = self._pd(private_key=KEY)
        assert isinstance(pd._signer, LocalAccountSigner)
        assert pd._signer.address == ADDR

    def test_accepts_custom_signer(self):
        signer = MagicMock()
        signer.address = ADDR
        pd = self._pd(signer=signer)
        assert pd._signer is signer

    def test_requires_key_or_signer(self):
        with pytest.raises(ValueError):
            self._pd()

    def test_requires_web3_provider_url(self):
        with patch("primedelta.primedelta.Web3"):
            with pytest.raises(ValueError):
                PrimeDelta(private_key=KEY)

    def test_rejects_both_key_and_signer(self):
        with pytest.raises(ValueError):
            self._pd(private_key=KEY, signer=MagicMock())

    def test_login_signs_through_signer(self):
        signer = MagicMock()
        signer.address = ADDR
        signer.sign_message.return_value = "deadbeef"
        pd = self._pd(signer=signer)
        pd._primedelta_client = MagicMock()
        pd._primedelta_client.get_nonce.return_value = "nonce123"

        pd.login()

        signer.sign_message.assert_called_once()
        siwe_message = signer.sign_message.call_args.args[0]
        assert ADDR in siwe_message
        assert pd._primedelta_client.login.call_args.kwargs["signature"] == "deadbeef"


OTHER_KEY = "0x" + "2" * 64


def _der_int(x: int) -> bytes:
    body = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
    if body[0] & 0x80:
        body = b"\x00" + body
    return b"\x02" + bytes([len(body)]) + body


class _FakeKms:
    def __init__(self, priv_hex, high_s=False, sign_priv_hex=None):
        from eth_keys import keys

        self._pub = keys.PrivateKey(bytes.fromhex(priv_hex.removeprefix("0x")))
        self._signer = keys.PrivateKey(
            bytes.fromhex((sign_priv_hex or priv_hex).removeprefix("0x"))
        )
        self._high_s = high_s

    def get_public_key(self, KeyId):
        return {"PublicKey": _SPKI_PREFIX + b"\x04" + self._pub.public_key.to_bytes()}

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):
        assert MessageType == "DIGEST"
        assert SigningAlgorithm == "ECDSA_SHA_256"
        sig = self._signer.sign_msg_hash(Message)
        s = _SECP256K1_N - sig.s if self._high_s else sig.s
        body = _der_int(sig.r) + _der_int(s)
        return {"Signature": b"\x30" + bytes([len(body)]) + body}


class TestKmsSigner:
    def _signer(self, **kwargs):
        return KmsSigner("dummy-arn", kms_client=_FakeKms(KEY, **kwargs))

    def test_address_matches_key(self):
        assert self._signer().address == ADDR

    def test_satisfies_signer_protocol(self):
        signer = self._signer()
        assert isinstance(signer, Signer)
        assert signer.fills_gas_and_nonce is False

    def test_sign_message_matches_local_and_recovers(self):
        message = "hello prime delta"
        signature = self._signer().sign_message(message)
        assert signature == LocalAccountSigner.from_key(KEY).sign_message(message)
        recovered = Account.recover_message(
            encode_defunct(text=message), signature=bytes.fromhex(signature)
        )
        assert recovered == ADDR

    def test_submit_transaction_matches_local_signing(self):
        captured = {}
        web3 = MagicMock()
        web3.eth.send_raw_transaction.side_effect = lambda raw: captured.__setitem__(
            "raw", raw
        )
        tx = {
            "from": ADDR,
            "to": ADDR,
            "value": 1,
            "gas": 21000,
            "gasPrice": 10**9,
            "nonce": 0,
            "chainId": 2028,
            "data": b"",
        }
        self._signer().submit_transaction(web3, dict(tx))
        assert Account.recover_transaction(captured["raw"]) == ADDR
        assert bytes(captured["raw"]) == bytes(
            Account.sign_transaction(tx, KEY).raw_transaction
        )

    def test_submit_transaction_rejects_mismatched_from(self):
        tx = {
            "from": "0x" + "9" * 40,
            "to": ADDR,
            "value": 1,
            "gas": 21000,
            "gasPrice": 10**9,
            "nonce": 0,
            "chainId": 2028,
            "data": b"",
        }
        with pytest.raises(ValueError):
            self._signer().submit_transaction(MagicMock(), tx)

    def test_rejects_malformed_public_key_der(self):
        class _BadKms(_FakeKms):
            def get_public_key(self, KeyId):
                return {"PublicKey": b"\x00" * 40}

        with pytest.raises(ValueError):
            KmsSigner("dummy-arn", kms_client=_BadKms(KEY))

    def test_normalizes_high_s_from_kms(self):
        message = "high-s case"
        low = self._signer(high_s=False).sign_message(message)
        high = self._signer(high_s=True).sign_message(message)
        assert low == high
        assert (
            Account.recover_message(
                encode_defunct(text=message), signature=bytes.fromhex(high)
            )
            == ADDR
        )

    def test_raises_when_signature_does_not_recover_key(self):
        signer = KmsSigner(
            "dummy-arn", kms_client=_FakeKms(KEY, sign_priv_hex=OTHER_KEY)
        )
        with pytest.raises(ValueError):
            signer.sign_message("x")
