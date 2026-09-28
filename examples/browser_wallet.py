from primedelta import BrowserSigner, PrimeDelta

TESTNET_CHAIN = {
    "chainId": "0x1cbd",
    "chainName": "PrimeDelta Testnet",
    "rpcUrls": ["https://chain.testnet.primedelta.io"],
    "nativeCurrency": {"name": "DEL", "symbol": "DEL", "decimals": 18},
}


def main() -> None:
    signer = BrowserSigner(chain=TESTNET_CHAIN)
    pd = PrimeDelta(
        signer=signer,
        web3_provider_url="https://chain.testnet.primedelta.io",
        network="testnet",
    )
    pd.login()
    print("logged in as:", signer.address)
    print("tradable stocks:", len(pd.stocks()))


if __name__ == "__main__":
    main()
