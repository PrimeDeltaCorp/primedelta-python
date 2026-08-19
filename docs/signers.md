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
