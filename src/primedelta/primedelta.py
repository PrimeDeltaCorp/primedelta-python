from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator, Optional, cast

from eth_abi import decode as abi_decode
from siwe import SiweMessage
from web3 import Web3
from web3.contract.contract import ContractFunction
from web3.exceptions import ContractLogicError
from web3.middleware import ExtraDataToPOAMiddleware
from web3.types import RPCEndpoint, TxParams

from primedelta.contracts import ContractRef, Contracts
from primedelta.dex.handlers import (
    _AMMPoolHandler,
    _DclexPoolHandler,
    _QuoteHandler,
    _RouterSwapHandler,
)
from primedelta.dex.params import (
    AddLiquidityParams,
    AMMAddLiquidity,
    AMMRemoveLiquidity,
    PoolType,
    PriceFeedAddLiquidity,
    PriceFeedRemoveLiquidity,
    RemoveLiquidityParams,
    SwapSide,
)
from primedelta.primedelta_client import APIError, NotLoggedIn, PrimeDeltaClient
from primedelta.settings import SIWE_MESSAGE, resolve_endpoints
from primedelta.signer import LocalAccountSigner, Signer
from primedelta.types import (
    AccountStatus,
    ApplicationSettings,
    BankDetails,
    ClaimableWithdrawal,
    Distribution,
    LPPosition,
    Message,
    Order,
    OrderCost,
    OrderSide,
    OrderStatus,
    Portfolio,
    PortfolioHistory,
    Price,
    Stock,
    Transfer,
)


class NotEnoughFunds(Exception):
    pass


class AccountNotVerified(Exception):
    pass


class DigitalIdentityAlreadyClaimed(Exception):
    pass


class WithdrawalNotFound(Exception):
    pass


class WdelNotConfigured(Exception):
    """Raised when a DEL/WDEL helper is called on a network whose config has no
    `wdel` address. Add it to `networks/<name>.json` to enable."""

    pass


class TransactionFailed(Exception):
    """A transaction submitted by the SDK reverted or failed to mine.

    Attributes:
        function_name: Solidity function the SDK tried to call.
        reason: Decoded revert reason if available (Error(string) or
            Panic(uint256)), otherwise the raw return data or original message.
        tx_hash: Hex-encoded tx hash if the transaction was submitted. None
            when the failure happened during pre-submit gas estimation.
        to: Target contract address (when known) — useful for replay via
            `cast call <to> <data> --from <sender> --rpc-url ...`.
        data: ABI-encoded calldata (when known) — pair with `to` to replay
            the failing call in a debugger / block explorer.
        trace: Best-effort `debug_traceCall` output if the node supports it.
    """

    def __init__(
        self,
        function_name: str,
        reason: str,
        tx_hash: Optional[str] = None,
        to: Optional[str] = None,
        data: Optional[str] = None,
        trace: Optional[Any] = None,
    ) -> None:
        self.function_name = function_name
        self.reason = reason
        self.tx_hash = tx_hash
        self.to = to
        self.data = data
        self.trace = trace
        parts = [f"{function_name}() reverted"]
        if tx_hash is not None:
            parts.append(f"tx_hash={tx_hash}")
        if to is not None:
            parts.append(f"to={to}")
        if data is not None:
            # Show the 4-byte selector + a short prefix so the line stays readable;
            # the full calldata is on `.data` for programmatic access.
            parts.append(f"selector={data[:10]} calldata_len={(len(data) - 2) // 2}B")
        parts.append(f"reason={reason}")
        super().__init__("; ".join(parts))


_ERROR_STRING_SELECTOR = "0x08c379a0"
_PANIC_SELECTOR = "0x4e487b71"


def _decode_revert(err: Exception) -> str:
    """Best-effort extraction of a human-readable revert reason."""
    data = getattr(err, "data", None)
    if isinstance(data, str) and data not in ("", "0x"):
        if data.startswith(_ERROR_STRING_SELECTOR):
            try:
                decoded = abi_decode(["string"], bytes.fromhex(data[10:]))[0]
                return f"Error({decoded!r})"
            except Exception:
                pass
        if data.startswith(_PANIC_SELECTOR):
            try:
                code = abi_decode(["uint256"], bytes.fromhex(data[10:]))[0]
                return f"Panic(0x{code:x})"
            except Exception:
                pass
        return f"raw_data={data}"
    message = getattr(err, "message", None)
    return message or str(err) or "no reason given"


def _deepest_trace_error(trace: Any) -> Optional[str]:
    """Walk a `debug_traceCall` (callTracer) tree and return the innermost error.

    Top-level callers report a generic "execution reverted"; the actual cause
    (e.g. "insufficient balance for transfer") often lives in a nested sub-call.
    """
    if not trace:
        return None
    deepest = None
    stack = [trace]
    while stack:
        node = stack.pop()
        err = None
        if hasattr(node, "get"):
            err = node.get("error")
            calls = node.get("calls") or []
        else:
            err = getattr(node, "error", None)
            calls = getattr(node, "calls", None) or []
        if err:
            deepest = err
        stack.extend(calls)
    return deepest


def _tx_hash_0x(tx_hash: Any) -> str:
    """Normalize a tx hash to a 0x-prefixed hex string. web3 v7's HexBytes.hex()
    returns bare hex; explorers, cast and the wider ecosystem expect the 0x."""
    return "0x" + tx_hash.hex().removeprefix("0x")


class PrimeDelta:
    def __init__(
        self,
        private_key: Optional[str] = None,
        web3_provider_url: Optional[str] = None,
        network: str = "dev",
        signer: Optional[Signer] = None,
    ) -> None:
        if web3_provider_url is None:
            raise ValueError("web3_provider_url is required")
        if signer is not None and private_key is not None:
            raise ValueError("Provide either private_key or signer, not both")
        if signer is None:
            if private_key is None:
                raise ValueError("Provide either private_key or signer")
            signer = LocalAccountSigner.from_key(private_key)
        self._signer: Signer = signer
        self._web3 = Web3(Web3.HTTPProvider(web3_provider_url))
        # Besu IBFT / Clique chains pack signer info into a 241-byte extraData
        # field; web3.py's default response formatter rejects anything > 32B.
        # Injecting the PoA middleware is a no-op on non-PoA chains.
        self._web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        # Backend + SIWE endpoints follow the network (env vars override).
        self._endpoints = resolve_endpoints(network)
        self._primedelta_client = PrimeDeltaClient(base_url=self._endpoints.base_url)
        # Contracts come from the SDK's bundled `networks/<name>.json` — not
        # from the backend. Pin addresses by editing that file.
        from primedelta import networks

        self._contracts: Contracts = networks.load(network)
        # Some Besu/PoA RPC nodes lag in updating the nonce counter even after
        # the previous tx's receipt is back. Track locally to avoid collisions
        # in chained submissions (e.g. approve → swap, mint → remove).
        self._next_nonce: Optional[int] = None
        self._dclex_handler = _DclexPoolHandler(
            web3=self._web3,
            account=self._signer,
            contracts_provider=self._get_contracts,
            send_tx=self._build_and_send_transaction,
        )
        self._amm_handler = _AMMPoolHandler(
            web3=self._web3,
            account=self._signer,
            contracts_provider=self._get_contracts,
            send_tx=self._build_and_send_transaction,
        )
        self._router_swapper = _RouterSwapHandler(
            web3=self._web3,
            account=self._signer,
            contracts_provider=self._get_contracts,
            signed_prices_fetcher=self._primedelta_client.get_signed_price_updates,
            send_tx=self._build_and_send_transaction,
        )
        self._quote_handler = _QuoteHandler(
            web3=self._web3,
            contracts_provider=self._get_contracts,
        )

    def _get_contracts(self) -> Contracts:
        return self._contracts

    def login(self) -> None:
        nonce = self._primedelta_client.get_nonce()
        issued_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        message = SiweMessage(
            domain=self._endpoints.siwe_domain,
            address=self._signer.address,
            statement=SIWE_MESSAGE,
            uri=self._endpoints.siwe_uri,
            version="1",
            chain_id=self._get_contracts().chain_id,
            nonce=nonce,
            issued_at=issued_at,
        ).prepare_message()
        signature = self._signer.sign_message(message)
        self._primedelta_client.login(message=message, signature=signature, nonce=nonce)

    def logged_in(self) -> bool:
        try:
            self.get_account_status()
        except NotLoggedIn:
            return False
        return True

    def logout(self) -> None:
        self._primedelta_client.logout()

    def claim_digital_identity(self) -> str:
        account_status = self._primedelta_client.get_account_status()
        if account_status == AccountStatus.DID_MINTED:
            raise DigitalIdentityAlreadyClaimed()
        if account_status != AccountStatus.VERIFIED:
            raise AccountNotVerified()

        signature = self._primedelta_client.create_digital_identity_signature()

        digital_identity = self._get_contracts().core.digital_identity
        digital_identity_contract_address = self._web3.to_checksum_address(
            digital_identity.address
        )
        digital_identity_contract = self._web3.eth.contract(
            address=digital_identity_contract_address, abi=digital_identity.abi
        )
        return self._build_and_send_transaction(
            digital_identity_contract.functions.mint(
                {
                    "account": self._signer.address,
                    "nonce": int.from_bytes(bytes.fromhex(signature.nonce), "big"),
                    "isPro": signature.is_pro,
                    "data": bytes.fromhex(signature.data),
                },
                bytes.fromhex(signature.signature),
            )
        )

    def get_account_status(self) -> AccountStatus:
        return self._primedelta_client.get_account_status()

    def verification_url(self) -> str:
        """Return the URL of the web page where the user completes KYC.

        Verification (uploading ID, taking a selfie, etc.) is performed in the
        web app, not via the SDK. After verification completes there, the
        backend marks the account as VERIFIED and the SDK can call
        `claim_digital_identity()` to mint the on-chain DID NFT.
        """
        return self._endpoints.app_url

    def open_verification_page(self) -> str:
        """Open the verification web page in the user's default browser.

        Returns the URL that was opened, so callers can fall back to printing
        it if `webbrowser.open` returned False (headless environments).
        """
        import webbrowser

        url = self.verification_url()
        webbrowser.open(url)
        return url

    def deposit_stablecoin(self, amount: int) -> str:
        account_status = self._primedelta_client.get_account_status()
        if account_status != AccountStatus.DID_MINTED:
            raise AccountNotVerified()

        signature = self._primedelta_client.get_deposit_stablecoin_signature(
            amount=amount, symbol="dUSD"
        )

        factory = self._get_contracts().core.factory
        factory_contract = self._web3.eth.contract(
            address=self._web3.to_checksum_address(factory.address), abi=factory.abi
        )
        return self._build_and_send_transaction(
            factory_contract.functions.burnStablecoin(
                {
                    "symbol": "dUSD",
                    "amount": int(signature.amount),
                    "account": self._signer.address,
                    "nonce": int(signature.nonce, 16),
                },
                bytes.fromhex(signature.signature.removeprefix("0x")),
            )
        )

    def request_stablecoin_withdrawal(self, amount: Decimal) -> int:
        account_status = self._primedelta_client.get_account_status()
        if account_status not in [AccountStatus.VERIFIED, AccountStatus.DID_MINTED]:
            raise AccountNotVerified()

        try:
            return self._primedelta_client.request_stablecoin_withdrawal(amount=amount)
        except APIError as exc:
            if exc.error_code == "INSUFFICIENT_FUNDS":
                raise NotEnoughFunds()
            raise

    def claim_stablecoin_withdrawal(self, withdrawal_id: int) -> str:
        withdrawal = self._get_claimable_withdrawal(withdrawal_id)
        signature = self._primedelta_client.get_withdraw_signature(
            withdrawal_id=withdrawal_id,
        )

        factory = self._get_contracts().core.factory
        factory_contract_address = self._web3.to_checksum_address(factory.address)
        factory_contract = self._web3.eth.contract(
            address=factory_contract_address, abi=factory.abi
        )
        return self._build_and_send_transaction(
            factory_contract.functions.mintStablecoin(
                {
                    "symbol": withdrawal.asset_type,
                    "amount": int(signature.amount),
                    "account": self._signer.address,
                    "nonce": int(signature.nonce, 16),
                },
                bytes.fromhex(signature.signature.removeprefix("0x")),
            )
        )

    def deposit_stock_token(self, stock_symbol: str, amount: int) -> str:
        account_status = self._primedelta_client.get_account_status()
        if account_status != AccountStatus.DID_MINTED:
            raise AccountNotVerified()

        signature = self._primedelta_client.get_deposit_stocks_signature(
            amount=amount,
            symbol=stock_symbol,
        )

        factory = self._get_contracts().core.factory
        factory_contract_address = self._web3.to_checksum_address(factory.address)
        factory_contract = self._web3.eth.contract(
            address=factory_contract_address, abi=factory.abi
        )
        return self._build_and_send_transaction(
            factory_contract.functions.burnStocks(
                {
                    "symbol": stock_symbol,
                    "amount": int(signature.amount),
                    "account": self._signer.address,
                    "nonce": int(signature.nonce, 16),
                },
                bytes.fromhex(signature.signature),
            )
        )

    def request_stock_withdrawal(self, stock_symbol: str, amount: int) -> int:
        account_status = self._primedelta_client.get_account_status()
        if account_status != AccountStatus.DID_MINTED:
            raise AccountNotVerified()

        try:
            return self._primedelta_client.request_stock_withdrawal(
                amount=amount,
                asset_type=stock_symbol,
            )
        except APIError as exc:
            if exc.error_code == "INSUFFICIENT_FUNDS":
                raise NotEnoughFunds()
            raise

    def claim_stock_withdrawal(self, withdrawal_id: int) -> str:
        withdrawal = self._get_claimable_withdrawal(withdrawal_id)
        signature = self._primedelta_client.get_withdraw_signature(
            withdrawal_id=withdrawal_id,
        )

        factory = self._get_contracts().core.factory
        factory_contract_address = self._web3.to_checksum_address(factory.address)
        factory_contract = self._web3.eth.contract(
            address=factory_contract_address, abi=factory.abi
        )
        return self._build_and_send_transaction(
            factory_contract.functions.mintStocks(
                {
                    "symbol": withdrawal.asset_type,
                    "amount": int(signature.amount),
                    "account": self._signer.address,
                    "nonce": int(signature.nonce, 16),
                },
                bytes.fromhex(signature.signature.removeprefix("0x")),
            )
        )

    def _get_claimable_withdrawal(self, withdrawal_id: int) -> ClaimableWithdrawal:
        claimable_withdrawals = self._primedelta_client.claimable_withdrawals()
        matching_withdrawals = [
            withdrawal
            for withdrawal in claimable_withdrawals
            if withdrawal.withdrawal_id == withdrawal_id
        ]
        if len(matching_withdrawals) == 0:
            raise WithdrawalNotFound()
        if len(matching_withdrawals) > 1:
            raise RuntimeError(
                "Received multiple claimable withdrawals with the same id"
            )
        return matching_withdrawals[0]

    def pending_transfers(
        self, page_number: int = 1, page_size: int = 1000
    ) -> list[Transfer]:
        return self._primedelta_client.get_pending_transfers(page_number, page_size)

    def closed_transfers(
        self, page_number: int = 1, page_size: int = 1000
    ) -> list[Transfer]:
        return self._primedelta_client.get_closed_transfers(page_number, page_size)

    def distributions(
        self, page_number: int = 1, page_size: int = 1000
    ) -> list[Distribution]:
        return self._primedelta_client.get_distributions(page_number, page_size)

    def get_stablecoin_available_balance(self) -> Decimal:
        return self._primedelta_client.portfolio().buying_power

    def get_stablecoin_total_balance(self) -> Decimal:
        return self._primedelta_client.portfolio().total_funds

    def get_stock_available_balance(self, symbol: str) -> Decimal:
        for stock_item in self._primedelta_client.portfolio().positions:
            if stock_item.symbol == symbol:
                return stock_item.available_to_sell
        return Decimal(0)

    def get_stock_total_balance(self, symbol: str) -> Decimal:
        for stock_item in self._primedelta_client.portfolio().positions:
            if stock_item.symbol == symbol:
                return stock_item.total_owned
        return Decimal(0)

    def get_onchain_stablecoin_balance(self) -> Decimal:
        """Read stablecoin balance from chain (bypasses backend indexer lag)."""
        stablecoin = self._get_contracts().core.stablecoin
        token = self._web3.eth.contract(
            address=self._web3.to_checksum_address(stablecoin.address),
            abi=stablecoin.abi,
        )
        raw = token.functions.balanceOf(self._signer.address).call()
        return Decimal(raw) / Decimal(10**6)

    def get_onchain_stock_balance(self, symbol: str) -> Decimal:
        """Read a stock token balance from chain (bypasses backend indexer lag).

        Useful immediately after a swap when the backend hasn't synced yet.
        """
        from primedelta.dex.handlers import _require_pool_abi, _resolve_stock_token

        contracts = self._get_contracts()
        stock_addr = _resolve_stock_token(self._web3, contracts, symbol)
        token = self._web3.eth.contract(
            address=self._web3.to_checksum_address(stock_addr),
            abi=_require_pool_abi(contracts, "erc20"),
        )
        raw = token.functions.balanceOf(self._signer.address).call()
        return Decimal(raw) / Decimal(10**18)

    def get_native_del_balance(self) -> Decimal:
        """Read native DEL balance from chain."""
        raw = self._web3.eth.get_balance(
            self._web3.to_checksum_address(self._signer.address)
        )
        return Decimal(raw) / Decimal(10**18)

    def wrap_del(self, amount: Decimal) -> str:
        """Wrap native DEL → WDEL by sending msg.value to `WDEL.deposit()`.

        The resulting WDEL is an AMM ERC20 — feed it into the regular swap
        methods using the symbol "WDEL".
        """
        wdel = self._require_wdel()
        wdel_contract = self._web3.eth.contract(
            address=self._web3.to_checksum_address(wdel.address), abi=wdel.abi
        )
        return self._build_and_send_transaction(
            wdel_contract.functions.deposit(),
            value=int(amount * Decimal(10**18)),
        )

    def unwrap_del(self, amount: Decimal) -> str:
        """Unwrap WDEL → native DEL via `WDEL.withdraw(amount)`."""
        wdel = self._require_wdel()
        wdel_contract = self._web3.eth.contract(
            address=self._web3.to_checksum_address(wdel.address), abi=wdel.abi
        )
        return self._build_and_send_transaction(
            wdel_contract.functions.withdraw(int(amount * Decimal(10**18))),
        )

    def _require_wdel(self) -> ContractRef:
        wdel = self._get_contracts().core.wdel
        if wdel is None:
            raise WdelNotConfigured()
        return wdel

    def portfolio(self) -> Portfolio:
        try:
            return self._primedelta_client.portfolio()
        except APIError as exc:
            if exc.error_code == "ACCOUNT_NOT_FOUND":
                raise AccountNotVerified()
            raise

    def claimable_withdrawals(self) -> list[ClaimableWithdrawal]:
        return self._primedelta_client.claimable_withdrawals()

    def send_limit_order(
        self,
        side: OrderSide,
        stock_symbol: str,
        amount: int,
        price_limit: Decimal,
        date_of_cancellation: Optional[date] = None,
    ) -> int:
        try:
            return self._primedelta_client.send_limit_order(
                amount=amount,
                asset_type=stock_symbol,
                order_side=side,
                price_limit=price_limit,
                date_of_cancellation=date_of_cancellation,
            )
        except APIError as exc:
            if exc.error_code == "INSUFFICIENT_FUNDS":
                raise NotEnoughFunds()
            raise

    def send_sell_market_order(self, stock_symbol: str, amount: int) -> int:
        try:
            return self._primedelta_client.send_sell_market_order(
                amount=amount,
                asset_type=stock_symbol,
            )
        except APIError as exc:
            if exc.error_code == "INSUFFICIENT_FUNDS":
                raise NotEnoughFunds()
            raise

    def cancel_order(self, order_id: int) -> None:
        return self._primedelta_client.cancel_order(order_id)

    def get_order_status(self, order_id: int) -> OrderStatus:
        return self._primedelta_client.get_order_status(order_id)

    def open_orders(self, page_number: int = 1, page_size: int = 1000) -> list[Order]:
        return self._primedelta_client.open_orders(page_number, page_size)

    def closed_orders(self, page_number: int = 1, page_size: int = 1000) -> list[Order]:
        return self._primedelta_client.closed_orders(page_number, page_size)

    def stocks(self) -> dict[str, Stock]:
        return self._primedelta_client.stocks()

    def prices_stream(self, symbols: Optional[list[str]] = None) -> Iterator[Price]:
        """Stream real-time price updates.

        When logged in, uses the broker's authenticated price stream.
        When not logged in, uses Pyth Hermes API for public price feeds.

        Args:
            symbols: List of stock symbols to stream prices for.
                     Only used when not logged in (Pyth stream).
                     If None, streams all available stocks.

        Raises:
            AccountNotVerified: If logged in but account is not verified.
                                Use pyth_prices_stream() or verify at https://app.primedelta.io
        """
        if self.logged_in():
            account_status = self._primedelta_client.get_account_status()
            if account_status not in [AccountStatus.VERIFIED, AccountStatus.DID_MINTED]:
                raise AccountNotVerified(
                    "Account not verified. Use pyth_prices_stream() for public prices "
                    "or verify your account at https://app.primedelta.io"
                )
            prices_stream_access_token = (
                self._primedelta_client.prices_stream_access_token()
            )
            return self._primedelta_client.prices_stream(prices_stream_access_token)
        else:
            if symbols is None:
                symbols = list(self.stocks().keys())
            return self._primedelta_client.pyth_prices_stream(symbols)

    def pyth_prices_stream(
        self, symbols: Optional[list[str]] = None
    ) -> Iterator[Price]:
        """Stream prices from Pyth Hermes API.

        This method does not require authentication and can be used when not logged in.

        Args:
            symbols: List of stock symbols to stream prices for.
                     If None, streams all available stocks.
        """
        if symbols is None:
            symbols = list(self.stocks().keys())
        return self._primedelta_client.pyth_prices_stream(symbols)

    def swap_exact_input(
        self,
        symbol: str,
        side: SwapSide,
        amount_in: Decimal,
        min_amount_out: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        self._require_logged_in_and_did_minted()
        return self._router_swapper.swap_exact_input(
            symbol, side, amount_in, min_amount_out, deadline_seconds, update_fee
        )

    def swap_exact_output(
        self,
        symbol: str,
        side: SwapSide,
        amount_out: Decimal,
        max_amount_in: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        self._require_logged_in_and_did_minted()
        return self._router_swapper.swap_exact_output(
            symbol, side, amount_out, max_amount_in, deadline_seconds, update_fee
        )

    def swap_token_to_token_exact_input(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_in: Decimal,
        min_amount_out: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        """Trade one non-dUSD token for another (routed through dUSD on chain).

        Use for AMM↔AMM, AMM↔stock, and stock↔stock swaps. For dUSD↔token
        swaps, use `swap_exact_input` with `SwapSide`.
        """
        self._require_logged_in_and_did_minted()
        return self._router_swapper.swap_token_to_token_exact_input(
            input_symbol,
            output_symbol,
            amount_in,
            min_amount_out,
            deadline_seconds,
            update_fee,
        )

    def swap_token_to_token_exact_output(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_out: Decimal,
        max_amount_in: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        """Exact-output cross-dex swap. See `swap_token_to_token_exact_input`."""
        self._require_logged_in_and_did_minted()
        return self._router_swapper.swap_token_to_token_exact_output(
            input_symbol,
            output_symbol,
            amount_out,
            max_amount_in,
            deadline_seconds,
            update_fee,
        )

    def add_liquidity(self, params: AddLiquidityParams) -> str:
        self._require_logged_in_and_did_minted()
        return self._handler_for(params.pool_type).add_liquidity(params)

    def remove_liquidity(self, params: RemoveLiquidityParams) -> str:
        return self._handler_for(params.pool_type).remove_liquidity(params)

    def collect_fees(self, position_id: int) -> str:
        self._require_logged_in_and_did_minted()
        return self._amm_handler.collect_fees(position_id)

    def increase_liquidity(
        self,
        position_id: int,
        amount_stock: Decimal,
        amount_stablecoin: Decimal,
        amount_stock_min: Decimal = Decimal(0),
        amount_stablecoin_min: Decimal = Decimal(0),
    ) -> str:
        self._require_logged_in_and_did_minted()
        return self._amm_handler.increase_liquidity(
            position_id,
            amount_stock,
            amount_stablecoin,
            amount_stock_min,
            amount_stablecoin_min,
        )

    def burn_position(self, position_id: int) -> str:
        self._require_logged_in_and_did_minted()
        return self._amm_handler.burn_position(position_id)

    def preview_fees(self, position_id: int) -> tuple[Decimal, Decimal]:
        """Preview an AMM (V3) position's currently collectable amounts as
        (stock, stablecoin) in human units, via a static `collect` simulation.
        Read-only (no login) but only for positions owned by this wallet — the
        NPM gates collect on ownership, so a third party's position_id reverts."""
        return self._amm_handler.preview_fees(position_id)

    def lp_positions(self) -> list[int]:
        """Return all AMM (V3) position NFT token IDs owned by the wallet."""
        npm = self._npm_contract()
        count = npm.functions.balanceOf(self._signer.address).call()
        return [
            npm.functions.tokenOfOwnerByIndex(self._signer.address, i).call()
            for i in range(count)
        ]

    def lp_position(self, position_id: int) -> LPPosition:
        """Read AMM (V3) position info for a given NFT token ID."""
        npm = self._npm_contract()
        p = npm.functions.positions(position_id).call()
        return LPPosition(
            token_id=position_id,
            token0=p[2],
            token1=p[3],
            fee=p[4],
            tick_lower=p[5],
            tick_upper=p[6],
            liquidity=p[7],
            tokens_owed_0=p[10],
            tokens_owed_1=p[11],
        )

    def _npm_contract(self) -> Any:
        from primedelta.dex.handlers import PositionManagerNotConfigured

        npm_ref = self._get_contracts().core.position_manager
        if npm_ref is None:
            raise PositionManagerNotConfigured()
        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(npm_ref.address),
            abi=npm_ref.abi,
        )

    def _handler_for(self, pool_type: PoolType) -> Any:
        if pool_type == PoolType.PRICE_FEED:
            return self._dclex_handler
        if pool_type == PoolType.AMM:
            return self._amm_handler
        raise ValueError(f"Unknown pool type: {pool_type}")

    def _require_logged_in_and_did_minted(self) -> None:
        account_status = self._primedelta_client.get_account_status()
        if account_status != AccountStatus.DID_MINTED:
            raise AccountNotVerified()

    def _send_with_nonce_retry(self, send_once: Callable[[], str]) -> str:
        # Besu's "pending" nonce occasionally lags behind the actual account
        # state after a fresh receipt. When the chain rejects "nonce too low"
        # it tells us the expected nonce in the error — parse it and retry.
        import re
        import time

        last_error: Optional[TransactionFailed] = None
        for attempt in range(5):
            try:
                return send_once()
            except TransactionFailed as e:
                if "nonce too low" not in (e.reason or "").lower():
                    raise
                last_error = e
                # The error tells us exactly what the chain expects next; pin
                # our local counter to that and let the chain settle briefly
                # before retrying so failed-status mined txs propagate.
                match = re.search(r"account nonce (\d+)", e.reason)
                if match:
                    self._next_nonce = int(match.group(1)) - 1  # reserve bumps +1
                else:
                    self._next_nonce = None
                time.sleep(1.0 + attempt)  # back off: 1s, 2s, 3s, 4s, 5s
        assert last_error is not None
        raise last_error

    def _build_and_send_transaction(
        self, contract_function: ContractFunction, value: int = 0
    ) -> str:
        return self._send_with_nonce_retry(
            lambda: self._build_and_send_transaction_once(contract_function, value)
        )

    def _build_and_send_transaction_once(
        self, contract_function: ContractFunction, value: int = 0
    ) -> str:
        fn_name = getattr(contract_function, "fn_name", None) or "<unknown>"
        to_address = getattr(contract_function, "address", None)
        try:
            calldata = contract_function._encode_transaction_data()
        except Exception:
            calldata = None
        if self._signer.fills_gas_and_nonce:
            transaction = {
                "from": self._signer.address,
                "to": to_address,
                "value": value,
                "data": calldata,
                "chainId": self._get_contracts().chain_id,
            }
        else:
            tx_params: dict[str, Any] = {
                "from": self._signer.address,
                "value": value,
                "gasPrice": self._web3.eth.gas_price,
                "nonce": self._reserve_nonce(),
                "gas": 5_000_000,
            }
            try:
                transaction = dict(
                    contract_function.build_transaction(cast(TxParams, tx_params))
                )
            except ContractLogicError as e:
                self._next_nonce = None
                trace = self._try_debug_trace_call(
                    {
                        "from": self._signer.address,
                        "to": to_address,
                        "data": calldata,
                        "value": hex(value) if value else "0x0",
                    }
                )
                reason = _decode_revert(e)
                deepest = _deepest_trace_error(trace)
                if deepest and deepest != reason and "revert" in reason.lower():
                    reason = f"{deepest} (top-level: {reason})"
                raise TransactionFailed(
                    fn_name,
                    reason,
                    to=to_address,
                    data=calldata,
                    trace=trace,
                ) from e

        tx_hash = self._signer.submit_transaction(self._web3, transaction)
        # Wait for the receipt so chained calls (e.g. approve → swap) see the
        # state change. Without this the next tx's gas estimation runs against
        # pre-approve state on chains with real block time (dev/prod), reverting.
        receipt = self._web3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] == 0:
            # Re-run as eth_call at the block BEFORE our tx mined to extract
            # the revert reason. Using the mined block itself would replay
            # against post-tx state — the account's nonce is already past
            # ours, and Besu would mis-report "Nonce too low" instead of the
            # actual revert.
            reason = "reverted with no reason"
            try:
                self._web3.eth.call(
                    cast(TxParams, transaction), receipt["blockNumber"] - 1
                )
            except ContractLogicError as e:
                reason = _decode_revert(e)
            except Exception as e:
                reason = str(e)
            trace = self._try_debug_trace_call(transaction)
            deepest = _deepest_trace_error(trace)
            if deepest and deepest != reason and "revert" in reason.lower():
                reason = f"{deepest} (top-level: {reason})"
            raise TransactionFailed(
                fn_name,
                reason,
                tx_hash=_tx_hash_0x(tx_hash),
                to=to_address,
                data=calldata,
                trace=trace,
            )
        return _tx_hash_0x(tx_hash)

    def _build_and_send_value_transaction(self, to: str, value: int) -> str:
        return self._send_with_nonce_retry(
            lambda: self._build_and_send_value_transaction_once(to, value)
        )

    def _build_and_send_value_transaction_once(self, to: str, value: int) -> str:
        to = self._web3.to_checksum_address(to)
        transaction = {
            "from": self._signer.address,
            "to": to,
            "value": value,
            "chainId": self._get_contracts().chain_id,
        }
        if not self._signer.fills_gas_and_nonce:
            transaction["gasPrice"] = self._web3.eth.gas_price
            transaction["nonce"] = self._reserve_nonce()
            transaction["gas"] = 21_000
        tx_hash = self._signer.submit_transaction(self._web3, transaction)
        receipt = self._web3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt["status"] == 0:
            raise TransactionFailed(
                "transfer", "reverted", tx_hash=_tx_hash_0x(tx_hash), to=to
            )
        return _tx_hash_0x(tx_hash)

    def _reserve_nonce(self) -> int:
        """Return the next nonce to use.

        Queries chain via `eth_getTransactionCount(pending)` each time. If the
        chain's view hasn't caught up since our last submission (some Besu/PoA
        nodes lag), bump past our last-used value. Never goes backwards.
        """
        chain_nonce: int = self._web3.eth.get_transaction_count(
            self._web3.to_checksum_address(self._signer.address),
            "pending",
        )
        if self._next_nonce is not None and chain_nonce <= self._next_nonce:
            chain_nonce = self._next_nonce + 1
        self._next_nonce = chain_nonce
        return chain_nonce

    def _try_debug_trace_call(self, tx: dict[str, Any]) -> Optional[Any]:
        """Attempt `debug_traceCall` for a richer call trace.

        Many public RPCs disable this. We swallow any failure so error
        reporting never raises a second exception.
        """
        try:
            return self._web3.manager.request_blocking(
                RPCEndpoint("debug_traceCall"),
                [tx, "latest", {"tracer": "callTracer"}],
            )
        except Exception:
            return None

    def is_market_open(self) -> bool:
        return self._primedelta_client.is_market_open()

    def messages(self) -> list[Message]:
        return self._primedelta_client.messages()

    def mark_message_read(self, message_id: int) -> None:
        self._primedelta_client.mark_message_read(message_id)

    def bank_details(self) -> BankDetails:
        return self._primedelta_client.bank_details()

    def request_fiat_withdrawal(self, amount: Decimal) -> int:
        try:
            return self._primedelta_client.request_fiat_withdrawal(amount)
        except APIError as exc:
            if exc.error_code == "INSUFFICIENT_FUNDS":
                raise NotEnoughFunds()
            raise

    def limit_buy_cost(
        self, stock_symbol: str, amount: int, price_limit: Decimal
    ) -> OrderCost:
        return self._primedelta_client.limit_order_cost(
            OrderSide.BUY, stock_symbol, amount, price_limit
        )

    def limit_sell_cost(
        self, stock_symbol: str, amount: int, price_limit: Decimal
    ) -> OrderCost:
        return self._primedelta_client.limit_order_cost(
            OrderSide.SELL, stock_symbol, amount, price_limit
        )

    def market_sell_cost(self, stock_symbol: str, amount: int) -> OrderCost:
        return self._primedelta_client.market_sell_cost(stock_symbol, amount)

    def swappable_symbols(self) -> list[str]:
        return self._primedelta_client.swappable_symbols()

    def application_settings(self) -> ApplicationSettings:
        return self._primedelta_client.application_settings()

    def portfolio_history(self, history_range: str) -> PortfolioHistory:
        return self._primedelta_client.portfolio_history(history_range)

    def digital_identity_id(self) -> Optional[int]:
        return self._primedelta_client.digital_identity_id()

    def quote_swap(
        self, symbol: str, side: SwapSide, amount: Decimal, exact: str = "input"
    ) -> Decimal:
        return self._quote_handler.quote_swap(symbol, side, amount, exact)

    def spot_price(self, symbol: str) -> Decimal:
        return self._quote_handler.spot_price(symbol)

    def allowance(self, token_symbol: str, spender: str) -> Decimal:
        address, decimals = self._token_ref(token_symbol)
        raw = (
            self._erc20(address)
            .functions.allowance(
                self._signer.address, self._web3.to_checksum_address(spender)
            )
            .call()
        )
        return Decimal(raw) / Decimal(10**decimals)

    def approve(self, token_symbol: str, spender: str, amount: Decimal) -> str:
        address, decimals = self._token_ref(token_symbol)
        return self._build_and_send_transaction(
            self._erc20(address).functions.approve(
                self._web3.to_checksum_address(spender),
                int(amount * Decimal(10**decimals)),
            )
        )

    def revoke_approval(self, token_symbol: str, spender: str) -> str:
        address, _ = self._token_ref(token_symbol)
        return self._build_and_send_transaction(
            self._erc20(address).functions.approve(
                self._web3.to_checksum_address(spender), 0
            )
        )

    def send_del(self, to: str, amount: Decimal) -> str:
        return self._build_and_send_value_transaction(to, int(amount * Decimal(10**18)))

    def _token_ref(self, token_symbol: str) -> tuple[str, int]:
        from primedelta.dex.handlers import _resolve_stock_token

        contracts = self._get_contracts()
        if token_symbol == "dUSD":
            return contracts.core.stablecoin.address, 6
        return _resolve_stock_token(self._web3, contracts, token_symbol), 18

    def _erc20(self, address: str) -> Any:
        from primedelta.dex.handlers import _require_pool_abi

        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(address),
            abi=_require_pool_abi(self._get_contracts(), "erc20"),
        )

    def did_token_id(self) -> Optional[int]:
        token_id = self._did_contract().functions.getId(self._signer.address).call()
        return token_id if token_id != 0 else None

    def is_pro(self) -> bool:
        token_id = self.did_token_id()
        if token_id is None:
            return False
        return self._did_contract().functions.isPro(token_id).call()

    def is_valid(self) -> bool:
        token_id = self.did_token_id()
        if token_id is None:
            return False
        return self._did_contract().functions.isValid(token_id).call()

    def _did_contract(self) -> Any:
        did = self._get_contracts().core.digital_identity
        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(did.address), abi=did.abi
        )
