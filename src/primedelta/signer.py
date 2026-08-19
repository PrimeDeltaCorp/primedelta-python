import json
from pathlib import Path
from typing import Any, Protocol, Union, runtime_checkable

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount


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
