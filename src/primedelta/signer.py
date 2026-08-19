import json
from pathlib import Path
from typing import Any, Protocol, Union, runtime_checkable

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_keys import keys


@runtime_checkable
class Signer(Protocol):
    """Wallet seam the SDK signs and broadcasts through.

    `fills_gas_and_nonce` is False for signers where the SDK builds a fully
    specified transaction (nonce, gas, gasPrice) and the signer only signs and
    broadcasts it, and True for wallet signers that fill nonce/gas and broadcast
    themselves (e.g. a browser extension).
    """

    fills_gas_and_nonce: bool

    @property
    def address(self) -> str:
        raise NotImplementedError

    def sign_message(self, message: str) -> str:
        raise NotImplementedError

    def submit_transaction(self, web3: Any, transaction: dict) -> Any:
        raise NotImplementedError


class LocalAccountSigner:
    fills_gas_and_nonce = False

    def __init__(self, account: LocalAccount) -> None:
        self._account = account

    @classmethod
    def from_key(cls, private_key: str) -> "LocalAccountSigner":
        return cls(Account.from_key(private_key))

    @classmethod
    def from_keystore(
        cls, path: Union[str, Path], password: str
    ) -> "LocalAccountSigner":
        keyfile = json.loads(Path(path).read_text())
        return cls(Account.from_key(Account.decrypt(keyfile, password)))

    @classmethod
    def from_mnemonic(
        cls, phrase: str, index: int = 0, passphrase: str = ""
    ) -> "LocalAccountSigner":
        Account.enable_unaudited_hdwallet_features()
        account = Account.from_mnemonic(
            phrase,
            passphrase=passphrase,
            account_path=f"m/44'/60'/0'/0/{index}",
        )
        return cls(account)

    @property
    def address(self) -> str:
        return self._account.address

    def sign_message(self, message: str) -> str:
        return self._account.sign_message(encode_defunct(text=message)).signature.hex()

    def submit_transaction(self, web3: Any, transaction: dict) -> Any:
        signed = self._account.sign_transaction(transaction)
        return web3.eth.send_raw_transaction(signed.raw_transaction)


_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SPKI_PREFIX = bytes.fromhex("3056301006072a8648ce3d020106052b8104000a034200")


def _pubkey_from_spki_der(der: bytes) -> bytes:
    if len(der) < 65 or der[-65] != 0x04:
        raise ValueError("unexpected KMS public key DER encoding")
    return der[-64:]


def _parse_der_signature(der: bytes) -> tuple:
    if not der or der[0] != 0x30:
        raise ValueError("malformed DER signature")
    index = 2
    if der[index] != 0x02:
        raise ValueError("malformed DER signature")
    index += 1
    r_len = der[index]
    index += 1
    r = int.from_bytes(der[index : index + r_len], "big")
    index += r_len
    if der[index] != 0x02:
        raise ValueError("malformed DER signature")
    index += 1
    s_len = der[index]
    index += 1
    s = int.from_bytes(der[index : index + s_len], "big")
    return r, s


def _low_s(s: int) -> int:
    return _SECP256K1_N - s if s > _SECP256K1_N // 2 else s


class KmsSigner:
    """Signer whose key stays inside AWS KMS — only 32-byte digests are sent out.

    Requires the optional ``primedelta[kms]`` extra (boto3). Pass an existing KMS
    client as ``kms_client``, or let the signer build one for ``region_name``.
    The key must be an asymmetric secp256k1 signing key (KeySpec ECC_SECG_P256K1,
    KeyUsage SIGN_VERIFY).
    """

    fills_gas_and_nonce = False

    def __init__(
        self, key_id: str, kms_client: Any = None, region_name: Any = None
    ) -> None:
        if kms_client is None:
            import boto3

            kms_client = boto3.client("kms", region_name=region_name)
        self._kms = kms_client
        self._key_id = key_id
        der = self._kms.get_public_key(KeyId=key_id)["PublicKey"]
        self._public_key = keys.PublicKey(_pubkey_from_spki_der(der))
        self._address = self._public_key.to_checksum_address()

    @property
    def address(self) -> str:
        return self._address

    def _sign_digest(self, digest: bytes) -> tuple:
        response = self._kms.sign(
            KeyId=self._key_id,
            Message=digest,
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        r, s = _parse_der_signature(response["Signature"])
        s = _low_s(s)
        for recovery_id in (0, 1):
            recovered = keys.Signature(
                vrs=(recovery_id, r, s)
            ).recover_public_key_from_msg_hash(digest)
            if recovered == self._public_key:
                return recovery_id, r, s
        raise ValueError("could not recover the KMS public key from its signature")

    def sign_message(self, message: str) -> str:
        from eth_account.messages import _hash_eip191_message

        digest = _hash_eip191_message(encode_defunct(text=message))
        recovery_id, r, s = self._sign_digest(digest)
        signature = (
            r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + recovery_id])
        )
        return signature.hex()

    def submit_transaction(self, web3: Any, transaction: dict) -> Any:
        from eth_account._utils.legacy_transactions import (
            encode_transaction,
            serializable_unsigned_transaction_from_dict,
        )

        unsigned = serializable_unsigned_transaction_from_dict(transaction)
        recovery_id, r, s = self._sign_digest(unsigned.hash())
        chain_id = transaction.get("chainId")
        v = recovery_id + (35 + 2 * chain_id if chain_id is not None else 27)
        encoded = encode_transaction(unsigned, vrs=(v, r, s))
        return web3.eth.send_raw_transaction(encoded)
