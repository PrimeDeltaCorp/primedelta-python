from primedelta import BrowserSigner, PrimeDelta

DEV_CHAIN = {
    "chainId": "0x7ec",
    "chainName": "PrimeDelta Dev",
    "rpcUrls": ["https://besu-dev.primedelta.io"],
    "nativeCurrency": {"name": "DEL", "symbol": "DEL", "decimals": 18},
}


def main() -> None:
    signer = BrowserSigner(chain=DEV_CHAIN)
    pd = PrimeDelta(
        signer=signer,
        web3_provider_url="https://besu-dev.primedelta.io",
        network="dev",
    )
    pd.login()
    print("logged in as:", signer.address)
    print("tradable stocks:", len(pd.stocks()))


if __name__ == "__main__":
    main()
