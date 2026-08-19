"""Ways to provision a signer for PrimeDelta.

The whole wallet dependency is a `Signer`: `address`, `sign_message` (SIWE) and
submit-a-transaction. Pick the one that fits your deployment; everything else in
the SDK is identical regardless of which signer you use.
"""

import os

from primedelta import (
    BrowserSigner,
    KmsSigner,
    LocalAccountSigner,
    PrimeDelta,
)

RPC = os.environ.get("PRIMEDELTA_PROVIDER_URL", "https://besu-dev.primedelta.io")


# 1. Raw private key (dev / scripts). `private_key=` is a thin wrapper for this.
def from_raw_key() -> PrimeDelta:
    signer = LocalAccountSigner.from_key(os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"])
    return PrimeDelta(signer=signer, web3_provider_url=RPC, network="dev")


# 2. Encrypted JSON keystore (UTC/geth format) — no plaintext key on disk.
def from_keystore() -> PrimeDelta:
    signer = LocalAccountSigner.from_keystore(
        os.environ["PRIMEDELTA_KEYSTORE_PATH"],
        os.environ["PRIMEDELTA_KEYSTORE_PASSWORD"],
    )
    return PrimeDelta(signer=signer, web3_provider_url=RPC, network="dev")


# 3. BIP-39 mnemonic + HD derivation index.
def from_mnemonic() -> PrimeDelta:
    signer = LocalAccountSigner.from_mnemonic(
        os.environ["PRIMEDELTA_MNEMONIC"], index=0
    )
    return PrimeDelta(signer=signer, web3_provider_url=RPC, network="dev")


# 4. AWS KMS — the private key never leaves the HSM (production posture).
#    Install the optional extra: `pip install "primedelta[kms]"`.
def from_kms() -> PrimeDelta:
    signer = KmsSigner(key_id=os.environ["PRIMEDELTA_KMS_KEY_ID"])
    return PrimeDelta(signer=signer, web3_provider_url=RPC, network="dev")


# 5. Browser wallet (MetaMask / any EIP-6963 provider) via a local loopback
#    bridge — the user signs in their own extension. See browser_wallet.py.
def from_browser() -> PrimeDelta:
    signer = BrowserSigner(
        chain={
            "chainId": "0x7ec",
            "chainName": "PrimeDelta Dev",
            "rpcUrls": [RPC],
            "nativeCurrency": {"name": "DEL", "symbol": "DEL", "decimals": 18},
        }
    )
    return PrimeDelta(signer=signer, web3_provider_url=RPC, network="dev")


# Switching networks is just `network=`; the backend + SIWE domain follow it,
# no extra env needed (env vars still override when set).
def on_testnet() -> PrimeDelta:
    signer = LocalAccountSigner.from_key(os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"])
    return PrimeDelta(
        signer=signer,
        web3_provider_url="https://chain-testnet.primedelta.io",
        network="testnet",
    )


if __name__ == "__main__":
    pd = from_raw_key()
    pd.login()
    print("logged in as:", pd._signer.address)
    pd.logout()
