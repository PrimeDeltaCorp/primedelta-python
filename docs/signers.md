# Wallet provisioning

`PrimeDelta` signs and broadcasts through a `Signer`. Pass one as `signer=`, or
pass a raw `private_key=` (a thin wrapper around `LocalAccountSigner.from_key`).

```python
from primedelta import PrimeDelta, LocalAccountSigner

pd = PrimeDelta(signer=LocalAccountSigner.from_key("0x..."), web3_provider_url=RPC)
```

## LocalAccountSigner

Holds the private key in process. Construct it from a raw key, an encrypted
keystore (Web3 Secret Storage / geth format), or a BIP-39 mnemonic.

```python
LocalAccountSigner.from_key("0xabc...")
LocalAccountSigner.from_keystore("/path/to/keystore.json", password)
LocalAccountSigner.from_mnemonic("word1 word2 ... word12", index=0)
```

`from_mnemonic` derives `m/44'/60'/0'/0/{index}` (Ethereum default); pass a
different `index` to select another account, or `passphrase=` for a BIP-39
passphrase.

## Provisioning the key from a secret store

Keep raw keys out of source and out of argv. Read them at runtime and hand the
value to `from_key`.

Environment variable:

```python
import os
from primedelta import LocalAccountSigner

signer = LocalAccountSigner.from_key(os.environ["PRIMEDELTA_PRIVATE_KEY"])
```

AWS Secrets Manager:

```python
import boto3
from primedelta import LocalAccountSigner

secret = boto3.client("secretsmanager").get_secret_value(SecretId="primedelta/key")
signer = LocalAccountSigner.from_key(secret["SecretString"])
```

GCP Secret Manager:

```python
from google.cloud import secretmanager
from primedelta import LocalAccountSigner

client = secretmanager.SecretManagerServiceClient()
name = "projects/PROJECT/secrets/primedelta-key/versions/latest"
key = client.access_secret_version(name=name).payload.data.decode()
signer = LocalAccountSigner.from_key(key)
```

HashiCorp Vault (KV v2):

```python
import hvac
from primedelta import LocalAccountSigner

vault = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
data = vault.secrets.kv.v2.read_secret_version(path="primedelta/key")["data"]["data"]
signer = LocalAccountSigner.from_key(data["private_key"])
```

## Browser wallet (MetaMask, …)

`BrowserSigner` signs through a browser extension wallet — no private key touches
the SDK. Each operation opens a single-use page on `127.0.0.1` that discovers the
wallet (EIP-6963) and runs `eth_requestAccounts` / `personal_sign` /
`eth_sendTransaction`; the wallet fills gas and nonce and broadcasts.

```python
from primedelta import PrimeDelta, BrowserSigner

pd = PrimeDelta(signer=BrowserSigner(), web3_provider_url=RPC)
pd.login()  # opens the browser to connect the wallet, then to sign
```

Pass `chain` (a `wallet_addEthereumChain` params dict) to switch the wallet to
the right network first — it is added if unknown:

```python
signer = BrowserSigner(chain={
    "chainId": "0x7ec",
    "chainName": "PrimeDelta Dev",
    "rpcUrls": ["https://besu.dev.primedelta.io"],
    "nativeCurrency": {"name": "DEL", "symbol": "DEL", "decimals": 18},
})
```

Interactive and desktop-only: every signature/transaction opens a browser tab you
approve in the wallet. The bridge binds loopback only, gates each round-trip with
a one-time state token, serves a single result then shuts down, and times out
(`timeout=`, default 180s). WalletConnect is not used (no maintained Python v2
library); this bridge covers the same "plug in my wallet" intent. For automation,
use `MockBrowserSigner` (signs locally with a dev key on the same wallet path).

## Hosted browser wallet (remote app)

`BrowserSigner` opens the user's *local* browser, so it only works where the SDK
and the user share a machine. For a **hosted** app — an MCP server or web backend
that can't reach the user's local browser — `RemoteBrowserSigner` serves the same
one-shot wallet page from a public HTTPS origin instead. It keeps the wallet
non-custodial (the user's own MetaMask/EIP-6963 wallet signs; no key on the
server) and reuses `BrowserSigner`'s page, state token, and tx shape.

The hosting app owns transport. `RemoteBrowserSigner` parks each pending
operation under a one-time `state` token and calls your `deliver(url)` callback
with `{base_url}/sign?state=<token>` (e.g. surfaced to the user through an MCP
url-mode elicitation). You wire two routes back to the signer:

```python
from primedelta import RemoteBrowserSigner

signer = RemoteBrowserSigner(
    base_url="https://signer.example",   # must be https:// (a localhost origin is allowed only for testing)
    deliver=send_url_to_user,            # hand the /sign?state=… URL to the user
    chain={"chainId": "0x7ec", "chainName": "PrimeDelta Dev",
           "rpcUrls": [RPC],
           "nativeCurrency": {"name": "DEL", "symbol": "DEL", "decimals": 18}},
)

# GET  /sign?state=<token>   -> return signer.render_page(state)   (the wallet HTML)
# POST /result?state=<token> -> signer.resolve(state, value, error)  (unblocks the call)
```

The delivered URL carries **only** the opaque token — the transaction/message
stays server-side and is surfaced only by `render_page`. The token is a bearer
capability, so `base_url` must be `https://` (a `localhost` origin is permitted
for tests). `render_page`/`resolve` are thread-safe and each pending call has its
own token, so one server can serve many concurrent users.

## Keys that never leave the HSM

`KmsSigner` keeps an AWS KMS-managed secp256k1 key inside the HSM and only sends
it 32-byte digests to sign — the private key is never materialized in process.
Install the optional extra:

```
pip install "primedelta[kms]"
```

```python
from primedelta import PrimeDelta, KmsSigner

signer = KmsSigner(key_id="arn:aws:kms:...:key/...", region_name="eu-central-1")
pd = PrimeDelta(signer=signer, web3_provider_url=RPC)
```

The KMS key must be an asymmetric secp256k1 signing key (KeySpec
`ECC_SECG_P256K1`, KeyUsage `SIGN_VERIFY`). Pass an existing boto3 client as
`kms_client=` to reuse credentials and config.
