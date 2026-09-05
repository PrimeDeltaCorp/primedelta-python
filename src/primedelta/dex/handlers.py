from decimal import Decimal
from typing import Any, Callable, Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

from primedelta.contracts import ContractRef, Contracts
from primedelta.dex.params import (
    AMMAddLiquidity,
    AMMRemoveLiquidity,
    PriceFeedAddLiquidity,
    PriceFeedRemoveLiquidity,
    SwapSide,
)

_STABLECOIN_DECIMALS = Decimal(10**6)
_STOCK_DECIMALS = Decimal(10**18)

# DEFAULT_FEE_TIER from DclexRouter.sol — the AMM pools are created with this.
_AMM_FEE_TIER = 3000


def _require_pool_abi(contracts: "Contracts", key: str) -> list[Any]:
    abi = contracts.pool_abis.get(key)
    if abi is None:
        raise RuntimeError(f"{key!r} ABI missing from /contracts/ payload")
    return abi


def _call_view(fn_name: str, call_fn: Callable[[], Any]) -> Any:
    """Run a `.call()` and rewrap reverts as TransactionFailed for context.

    Without this, a deep view-call revert surfaces as web3's ContractLogicError
    with a useless `0x` data field — the SDK caller can't tell which function
    failed without parsing the stack trace.
    """
    from primedelta.primedelta import TransactionFailed, _decode_revert

    try:
        return call_fn()
    except ContractLogicError as e:
        raise TransactionFailed(fn_name, _decode_revert(e)) from e


# Symbol->address and stock->pool are IMMUTABLE topology (a token/pool address
# never changes for a deployed network), so we memoize them per (chain_id, key).
# This turns the on-chain `allStockTokens()` scan for AMM-only tokens
# (AMMT1/AMMT2/WDEL, ~45 `symbol()` reads) into a one-time cost instead of paying
# it on every quote/spot/swap — and makes those calls robust to the dev gateway
# occasionally serving an inconsistent read that would otherwise spuriously raise
# PoolNotFound. Only successful resolutions are cached (a miss retries next call).
_STOCK_ADDR_CACHE: dict[tuple[int, str], str] = {}
_POOL_ADDR_CACHE: dict[tuple[int, str], str] = {}


def _clear_resolution_cache() -> None:
    """Drop the memoized symbol->address / stock->pool resolutions. Addresses are
    immutable per network, so this is only needed after a redeploy in the same
    process (or between tests)."""
    _STOCK_ADDR_CACHE.clear()
    _POOL_ADDR_CACHE.clear()


def _resolve_stock_token(web3: Web3, contracts: "Contracts", symbol: str) -> str:
    """Resolve a stock symbol to its on-chain token address.

    Prefers the backend's `/contracts/` pools (fast dict lookup). Falls back
    to enumerating `Router.allStockTokens()` on-chain so AMM-only tokens that
    aren't synced to the backend DB (AMMT1/AMMT2/WDEL) still resolve. This
    mirrors how the DEX frontend (primedelta-dex/src/registry.ts) discovers
    tokens. The resolved address is memoized (immutable per network).
    """
    cache_key = (contracts.chain_id, symbol)
    cached = _STOCK_ADDR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    pool = contracts.pools.get(symbol)
    if pool is not None:
        _STOCK_ADDR_CACHE[cache_key] = pool.stock_token_address
        return pool.stock_token_address
    router_ref = contracts.core.dex_router
    if router_ref is None:
        raise PoolNotFound(symbol)
    addr = _resolve_via_router(web3, contracts, router_ref, symbol)
    if addr is None:
        raise PoolNotFound(symbol)
    _STOCK_ADDR_CACHE[cache_key] = addr
    return addr


def _resolve_via_router(
    web3: Web3, contracts: "Contracts", router_ref: ContractRef, symbol: str
) -> Optional[str]:
    erc20_abi = _require_pool_abi(contracts, "erc20")
    try:
        router = web3.eth.contract(
            address=web3.to_checksum_address(router_ref.address),
            abi=router_ref.abi,
        )
        all_tokens = router.functions.allStockTokens().call()
    except Exception:
        return None
    for addr in all_tokens:
        try:
            stock = web3.eth.contract(
                address=web3.to_checksum_address(addr),
                abi=erc20_abi,
            )
            if stock.functions.symbol().call() == symbol:
                return addr
        except Exception:
            continue
    return None


class PoolNotFound(Exception):
    pass


class RouterNotConfigured(Exception):
    pass


class PositionManagerNotConfigured(Exception):
    pass


class QuoterNotConfigured(Exception):
    pass


def _lookup_amm_pool_address(
    web3: Web3, contracts: "Contracts", stock_token_addr: str
) -> str:
    cache_key = (contracts.chain_id, stock_token_addr.lower())
    cached = _POOL_ADDR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    npm_ref = contracts.core.position_manager
    if npm_ref is None:
        raise PositionManagerNotConfigured()
    npm = web3.eth.contract(
        address=web3.to_checksum_address(npm_ref.address), abi=npm_ref.abi
    )
    factory_addr = _call_view(
        "NonfungiblePositionManager.factory",
        lambda: npm.functions.factory().call(),
    )
    factory = web3.eth.contract(
        address=web3.to_checksum_address(factory_addr),
        abi=_require_pool_abi(contracts, "univ3_factory"),
    )
    pool_addr = _call_view(
        "UniswapV3Factory.getPool",
        lambda: factory.functions.getPool(
            web3.to_checksum_address(stock_token_addr),
            web3.to_checksum_address(contracts.core.stablecoin.address),
            _AMM_FEE_TIER,
        ).call(),
    )
    if int(pool_addr, 16) == 0:
        raise PoolNotFound(f"no AMM pool registered for {stock_token_addr}")
    _POOL_ADDR_CACHE[cache_key] = pool_addr
    return pool_addr


class _RouterSwapHandler:
    def __init__(
        self,
        web3: Web3,
        account: Any,
        contracts_provider: Callable[[], Contracts],
        signed_prices_fetcher: Callable[[list[str]], list[bytes]],
        send_tx: Callable[..., str],
    ) -> None:
        self._web3 = web3
        self._account = account
        self._contracts_provider = contracts_provider
        self._signed_prices_fetcher = signed_prices_fetcher
        self._send_tx = send_tx

    def swap_exact_input(
        self,
        symbol: str,
        side: SwapSide,
        amount_in: Decimal,
        min_amount_out: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        contracts = self._contracts_provider()
        router_ref = self._require_router(contracts)
        stock_token_addr = self._require_stock_token(contracts, symbol)

        update_data = self._fetch_pyth_update_data(symbol)
        deadline = self._now() + deadline_seconds
        router = self._contract(router_ref)

        if side == SwapSide.STABLECOIN_TO_STOCK:
            amount_in_units = int(amount_in * _STABLECOIN_DECIMALS)
            min_out_units = int(min_amount_out * _STOCK_DECIMALS)
            self._approve(
                contracts.core.stablecoin, router_ref.address, amount_in_units
            )
            tx_function = router.functions.buyExactInput(
                stock_token_addr,
                amount_in_units,
                min_out_units,
                deadline,
                update_data,
            )
        else:
            amount_in_units = int(amount_in * _STOCK_DECIMALS)
            min_out_units = int(min_amount_out * _STABLECOIN_DECIMALS)
            self._approve_stock(stock_token_addr, router_ref.address, amount_in_units)
            tx_function = router.functions.sellExactInput(
                stock_token_addr,
                amount_in_units,
                min_out_units,
                deadline,
                update_data,
            )
        msg_value = self._resolve_msg_value(contracts, update_data, update_fee)
        return self._send_tx(tx_function, value=msg_value)

    def swap_exact_output(
        self,
        symbol: str,
        side: SwapSide,
        amount_out: Decimal,
        max_amount_in: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        contracts = self._contracts_provider()
        router_ref = self._require_router(contracts)
        stock_token_addr = self._require_stock_token(contracts, symbol)

        update_data = self._fetch_pyth_update_data(symbol)
        deadline = self._now() + deadline_seconds
        router = self._contract(router_ref)

        if side == SwapSide.STABLECOIN_TO_STOCK:
            amount_out_units = int(amount_out * _STOCK_DECIMALS)
            max_in_units = int(max_amount_in * _STABLECOIN_DECIMALS)
            self._approve(contracts.core.stablecoin, router_ref.address, max_in_units)
            tx_function = router.functions.buyExactOutput(
                stock_token_addr,
                amount_out_units,
                max_in_units,
                deadline,
                update_data,
            )
        else:
            amount_out_units = int(amount_out * _STABLECOIN_DECIMALS)
            max_in_units = int(max_amount_in * _STOCK_DECIMALS)
            self._approve_stock(stock_token_addr, router_ref.address, max_in_units)
            tx_function = router.functions.sellExactOutput(
                stock_token_addr,
                amount_out_units,
                max_in_units,
                deadline,
                update_data,
            )
        msg_value = self._resolve_msg_value(contracts, update_data, update_fee)
        return self._send_tx(tx_function, value=msg_value)

    def swap_token_to_token_exact_input(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_in: Decimal,
        min_amount_out: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        """Cross-dex swap: trade one non-dUSD token for another, routed through dUSD.

        Wraps the router's `swapExactInput(inputToken, outputToken, ...)` which
        internally does input→dUSD→output. Both legs may be either custom
        (pricefeed) or AMM pools; the router picks per-token. Pyth update data
        is fetched for both symbols since either side may host a custom pool.
        """
        if input_symbol == output_symbol:
            raise ValueError("input_symbol and output_symbol must differ")
        contracts = self._contracts_provider()
        router_ref = self._require_router(contracts)
        input_token_addr = self._require_stock_token(contracts, input_symbol)
        output_token_addr = self._require_stock_token(contracts, output_symbol)

        update_data = self._fetch_pyth_update_data_for([input_symbol, output_symbol])
        deadline = self._now() + deadline_seconds
        router = self._contract(router_ref)

        amount_in_units = int(amount_in * _STOCK_DECIMALS)
        min_out_units = int(min_amount_out * _STOCK_DECIMALS)
        self._approve_stock(input_token_addr, router_ref.address, amount_in_units)
        tx_function = router.functions.swapExactInput(
            input_token_addr,
            output_token_addr,
            amount_in_units,
            min_out_units,
            deadline,
            update_data,
        )
        msg_value = self._resolve_msg_value(contracts, update_data, update_fee)
        return self._send_tx(tx_function, value=msg_value)

    def swap_token_to_token_exact_output(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_out: Decimal,
        max_amount_in: Decimal,
        deadline_seconds: int = 600,
        update_fee: int = 0,
    ) -> str:
        """Cross-dex swap, exact-output variant. See `swap_token_to_token_exact_input`."""
        if input_symbol == output_symbol:
            raise ValueError("input_symbol and output_symbol must differ")
        contracts = self._contracts_provider()
        router_ref = self._require_router(contracts)
        input_token_addr = self._require_stock_token(contracts, input_symbol)
        output_token_addr = self._require_stock_token(contracts, output_symbol)

        update_data = self._fetch_pyth_update_data_for([input_symbol, output_symbol])
        deadline = self._now() + deadline_seconds
        router = self._contract(router_ref)

        amount_out_units = int(amount_out * _STOCK_DECIMALS)
        max_in_units = int(max_amount_in * _STOCK_DECIMALS)
        self._approve_stock(input_token_addr, router_ref.address, max_in_units)
        tx_function = router.functions.swapExactOutput(
            input_token_addr,
            output_token_addr,
            amount_out_units,
            max_in_units,
            deadline,
            update_data,
        )
        msg_value = self._resolve_msg_value(contracts, update_data, update_fee)
        return self._send_tx(tx_function, value=msg_value)

    def _require_router(self, contracts: Contracts) -> ContractRef:
        if contracts.core.dex_router is None:
            raise RouterNotConfigured()
        return contracts.core.dex_router

    def _require_stock_token(self, contracts: Contracts, symbol: str) -> str:
        return _resolve_stock_token(self._web3, contracts, symbol)

    def _fetch_pyth_update_data(self, symbol: str) -> list[bytes]:
        # Backend's `/signed-prices/` returns 117-byte FIOracle-format updates
        # (feedId + price + expo + publishTime + v + r + s) signed by the
        # backend's trusted signer. The pool's oracle verifies this signature,
        # so we cannot substitute Hermes/Pyth bytes.
        return self._signed_prices_fetcher([symbol])

    def _fetch_pyth_update_data_for(self, symbols: list[str]) -> list[bytes]:
        # Cross-dex routes may touch a custom pool on either leg; fetch signed
        # prices for both symbols (de-duped, order preserved).
        seen: set[str] = set()
        unique: list[str] = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return self._signed_prices_fetcher(unique)

    def _resolve_msg_value(
        self,
        contracts: Contracts,
        update_data: list[bytes],
        explicit_value: int,
    ) -> int:
        # Caller-supplied value wins so users can override (e.g. test setups).
        if explicit_value:
            return explicit_value
        oracle_ref = contracts.core.oracle
        if oracle_ref is None or not update_data:
            return 0
        # Pool forwards msg.value as the oracle update fee; query the oracle for
        # the exact amount it expects so the inner call doesn't revert with
        # "insufficient balance for transfer". FIOracle returns 0 (no fee).
        oracle = self._web3.eth.contract(
            address=self._web3.to_checksum_address(oracle_ref.address),
            abi=oracle_ref.abi,
        )
        return _call_view(
            "Oracle.getUpdateFee",
            lambda: oracle.functions.getUpdateFee(update_data).call(),
        )

    def _now(self) -> int:
        return int(self._web3.eth.get_block("latest")["timestamp"])

    def _contract(self, ref: ContractRef) -> Any:
        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(ref.address), abi=ref.abi
        )

    def _approve(self, token_ref: ContractRef, spender: str, amount: int) -> None:
        token = self._contract(token_ref)
        self._send_tx(
            token.functions.approve(self._web3.to_checksum_address(spender), amount)
        )

    def _approve_stock(
        self, stock_token_address: str, spender: str, amount: int
    ) -> None:
        token = self._erc20_at(stock_token_address)
        self._send_tx(
            token.functions.approve(self._web3.to_checksum_address(spender), amount)
        )

    def _erc20_at(self, address: str) -> Any:
        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(address),
            abi=_require_pool_abi(self._contracts_provider(), "erc20"),
        )


class _DclexPoolHandler:
    def __init__(
        self,
        web3: Web3,
        account: Any,
        contracts_provider: Callable[[], Contracts],
        send_tx: Callable[..., str],
    ) -> None:
        self._web3 = web3
        self._account = account
        self._contracts_provider = contracts_provider
        self._send_tx = send_tx

    def add_liquidity(self, params: PriceFeedAddLiquidity) -> str:
        contracts = self._contracts_provider()
        router_ref = self._require_router(contracts)
        stock_token_addr = self._require_stock_token(contracts, params.symbol)

        pool_address = self._lookup_dclex_pool(router_ref, stock_token_addr)
        pool = self._dclex_pool(contracts, pool_address)

        liquidity_units = int(params.liquidity_amount)
        max_stock_units = int(params.max_stock_amount * _STOCK_DECIMALS)
        max_stablecoin_units = int(params.max_stablecoin_amount * _STABLECOIN_DECIMALS)

        # Approve user-specified caps; pool takes proportional amount and reverts
        # if it would exceed allowance (chain-enforced slippage bound).
        self._approve_at(stock_token_addr, pool_address, max_stock_units)
        self._approve(contracts.core.stablecoin, pool_address, max_stablecoin_units)
        return self._send_tx(pool.functions.addLiquidity(liquidity_units))

    def remove_liquidity(self, params: PriceFeedRemoveLiquidity) -> str:
        contracts = self._contracts_provider()
        router_ref = self._require_router(contracts)
        stock_token_addr = self._require_stock_token(contracts, params.symbol)

        pool_address = self._lookup_dclex_pool(router_ref, stock_token_addr)
        pool = self._dclex_pool(contracts, pool_address)
        liquidity_units = int(params.liquidity_amount)
        return self._send_tx(pool.functions.removeLiquidity(liquidity_units))

    def _require_router(self, contracts: Contracts) -> ContractRef:
        if contracts.core.dex_router is None:
            raise RouterNotConfigured()
        return contracts.core.dex_router

    def _require_stock_token(self, contracts: Contracts, symbol: str) -> str:
        return _resolve_stock_token(self._web3, contracts, symbol)

    def _lookup_dclex_pool(self, router_ref: ContractRef, stock_token_addr: str) -> str:
        router = self._web3.eth.contract(
            address=self._web3.to_checksum_address(router_ref.address),
            abi=router_ref.abi,
        )
        pool_addr = _call_view(
            "DclexRouter.stockTokenToPool",
            lambda: router.functions.stockTokenToPool(
                self._web3.to_checksum_address(stock_token_addr)
            ).call(),
        )
        if int(pool_addr, 16) == 0:
            raise PoolNotFound(f"no DCLEX pool registered for {stock_token_addr}")
        return pool_addr

    def _dclex_pool(self, contracts: Contracts, pool_address: str) -> Any:
        abi = contracts.pool_abis.get("dclex_pool")
        if abi is None:
            raise RuntimeError("dclex_pool ABI missing from /contracts/ payload")
        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(pool_address), abi=abi
        )

    def _approve(self, token_ref: ContractRef, spender: str, amount: int) -> None:
        token = self._web3.eth.contract(
            address=self._web3.to_checksum_address(token_ref.address), abi=token_ref.abi
        )
        self._send_tx(
            token.functions.approve(self._web3.to_checksum_address(spender), amount)
        )

    def _approve_at(self, token_address: str, spender: str, amount: int) -> None:
        token = self._web3.eth.contract(
            address=self._web3.to_checksum_address(token_address),
            abi=_require_pool_abi(self._contracts_provider(), "erc20"),
        )
        self._send_tx(
            token.functions.approve(self._web3.to_checksum_address(spender), amount)
        )


class _AMMPoolHandler:
    def __init__(
        self,
        web3: Web3,
        account: Any,
        contracts_provider: Callable[[], Contracts],
        send_tx: Callable[..., str],
    ) -> None:
        self._web3 = web3
        self._account = account
        self._contracts_provider = contracts_provider
        self._send_tx = send_tx

    def add_liquidity(self, params: AMMAddLiquidity) -> str:
        contracts = self._contracts_provider()
        npm_ref = self._require_npm(contracts)
        stock_token_addr = self._require_stock_token(contracts, params.symbol)

        pool_address = self._lookup_amm_pool(npm_ref, contracts, stock_token_addr)
        token0, token1, fee = self._read_pool_tokens(contracts, pool_address)

        amounts = self._map_amounts(
            stock_token_addr,
            token0,
            stock=params.amount_stock_desired,
            stablecoin=params.amount_stablecoin_desired,
        )
        amounts_min = self._map_amounts(
            stock_token_addr,
            token0,
            stock=params.amount_stock_min,
            stablecoin=params.amount_stablecoin_min,
        )

        npm = self._contract(npm_ref)
        self._approve_at(token0, npm_ref.address, amounts[0])
        self._approve_at(token1, npm_ref.address, amounts[1])

        deadline = self._now() + 600
        return self._send_tx(
            npm.functions.mint(
                {
                    "token0": self._web3.to_checksum_address(token0),
                    "token1": self._web3.to_checksum_address(token1),
                    "fee": fee,
                    "tickLower": params.tick_lower,
                    "tickUpper": params.tick_upper,
                    "amount0Desired": amounts[0],
                    "amount1Desired": amounts[1],
                    "amount0Min": amounts_min[0],
                    "amount1Min": amounts_min[1],
                    "recipient": self._account.address,
                    "deadline": deadline,
                }
            )
        )

    def remove_liquidity(self, params: AMMRemoveLiquidity) -> str:
        contracts = self._contracts_provider()
        npm_ref = self._require_npm(contracts)
        npm = self._contract(npm_ref)
        deadline = self._now() + 600

        position = _call_view(
            "NonfungiblePositionManager.positions",
            lambda: npm.functions.positions(params.position_id).call(),
        )
        token0 = position[2]
        amount_stock_min_units = int(params.amount_stock_min * _STOCK_DECIMALS)
        amount_stablecoin_min_units = int(
            params.amount_stablecoin_min * _STABLECOIN_DECIMALS
        )
        if token0.lower() == contracts.core.stablecoin.address.lower():
            amount0_min, amount1_min = (
                amount_stablecoin_min_units,
                amount_stock_min_units,
            )
        else:
            amount0_min, amount1_min = (
                amount_stock_min_units,
                amount_stablecoin_min_units,
            )

        max_uint128 = (1 << 128) - 1
        decrease_call = npm.encode_abi(
            abi_element_identifier="decreaseLiquidity",
            args=[
                (
                    params.position_id,
                    params.liquidity,
                    amount0_min,
                    amount1_min,
                    deadline,
                )
            ],
        )
        collect_call = npm.encode_abi(
            abi_element_identifier="collect",
            args=[
                (
                    params.position_id,
                    self._account.address,
                    max_uint128,
                    max_uint128,
                )
            ],
        )
        return self._send_tx(npm.functions.multicall([decrease_call, collect_call]))

    def collect_fees(self, position_id: int) -> str:
        contracts = self._contracts_provider()
        npm_ref = self._require_npm(contracts)
        npm = self._contract(npm_ref)
        max_uint128 = (1 << 128) - 1
        return self._send_tx(
            npm.functions.collect(
                {
                    "tokenId": position_id,
                    "recipient": self._account.address,
                    "amount0Max": max_uint128,
                    "amount1Max": max_uint128,
                }
            )
        )

    def increase_liquidity(
        self,
        position_id: int,
        amount_stock: Decimal,
        amount_stablecoin: Decimal,
        amount_stock_min: Decimal = Decimal(0),
        amount_stablecoin_min: Decimal = Decimal(0),
    ) -> str:
        contracts = self._contracts_provider()
        npm_ref = self._require_npm(contracts)
        npm = self._contract(npm_ref)

        position = _call_view(
            "NonfungiblePositionManager.positions",
            lambda: npm.functions.positions(position_id).call(),
        )
        token0, token1 = position[2], position[3]
        stablecoin = contracts.core.stablecoin.address
        stock_token_addr = token1 if token0.lower() == stablecoin.lower() else token0

        amounts = self._map_amounts(
            stock_token_addr, token0, stock=amount_stock, stablecoin=amount_stablecoin
        )
        amounts_min = self._map_amounts(
            stock_token_addr,
            token0,
            stock=amount_stock_min,
            stablecoin=amount_stablecoin_min,
        )
        self._approve_at(token0, npm_ref.address, amounts[0])
        self._approve_at(token1, npm_ref.address, amounts[1])

        deadline = self._now() + 600
        return self._send_tx(
            npm.functions.increaseLiquidity(
                {
                    "tokenId": position_id,
                    "amount0Desired": amounts[0],
                    "amount1Desired": amounts[1],
                    "amount0Min": amounts_min[0],
                    "amount1Min": amounts_min[1],
                    "deadline": deadline,
                }
            )
        )

    def burn_position(self, position_id: int) -> str:
        contracts = self._contracts_provider()
        npm_ref = self._require_npm(contracts)
        npm = self._contract(npm_ref)
        deadline = self._now() + 600

        position = _call_view(
            "NonfungiblePositionManager.positions",
            lambda: npm.functions.positions(position_id).call(),
        )
        liquidity = position[7]
        max_uint128 = (1 << 128) - 1

        calls = []
        if liquidity > 0:
            calls.append(
                npm.encode_abi(
                    abi_element_identifier="decreaseLiquidity",
                    args=[(position_id, liquidity, 0, 0, deadline)],
                )
            )
        calls.append(
            npm.encode_abi(
                abi_element_identifier="collect",
                args=[(position_id, self._account.address, max_uint128, max_uint128)],
            )
        )
        calls.append(npm.encode_abi(abi_element_identifier="burn", args=[position_id]))
        return self._send_tx(npm.functions.multicall(calls))

    def preview_fees(self, position_id: int) -> tuple[Decimal, Decimal]:
        # Statically simulate collect(max, max) — an eth_call against the live
        # NPM returns what the position could collect right now (accrued fees
        # already booked to tokensOwed) without moving state. Returned as
        # (stock, stablecoin) in human units.
        contracts = self._contracts_provider()
        npm_ref = self._require_npm(contracts)
        npm = self._contract(npm_ref)
        max_uint128 = (1 << 128) - 1

        position = _call_view(
            "NonfungiblePositionManager.positions",
            lambda: npm.functions.positions(position_id).call(),
        )
        token0 = position[2]
        amount0, amount1 = _call_view(
            "NonfungiblePositionManager.collect(static)",
            # `from` must be the position owner: NPM.collect gates on
            # isAuthorizedForToken, and a static call defaults msg.sender to
            # the zero address, which fails that check ("Not approved").
            lambda: npm.functions.collect(
                {
                    "tokenId": position_id,
                    "recipient": self._account.address,
                    "amount0Max": max_uint128,
                    "amount1Max": max_uint128,
                }
            ).call({"from": self._account.address}),
        )
        stablecoin = contracts.core.stablecoin.address
        if token0.lower() == stablecoin.lower():
            stablecoin_units, stock_units = amount0, amount1
        else:
            stock_units, stablecoin_units = amount0, amount1
        return (
            Decimal(stock_units) / _STOCK_DECIMALS,
            Decimal(stablecoin_units) / _STABLECOIN_DECIMALS,
        )

    def _require_npm(self, contracts: Contracts) -> ContractRef:
        if contracts.core.position_manager is None:
            raise PositionManagerNotConfigured()
        return contracts.core.position_manager

    def _require_router(self, contracts: Contracts) -> ContractRef:
        if contracts.core.dex_router is None:
            raise RouterNotConfigured()
        return contracts.core.dex_router

    def _require_stock_token(self, contracts: Contracts, symbol: str) -> str:
        return _resolve_stock_token(self._web3, contracts, symbol)

    def _lookup_amm_pool(
        self, npm_ref: ContractRef, contracts: Contracts, stock_token_addr: str
    ) -> str:
        # The router's `stockToAMMPool` getter isn't always exposed in deployed
        # bytecode (older versions). Going through the NPM's V3 factory works
        # uniformly: NPM.factory().getPool(stock, stablecoin, DEFAULT_FEE_TIER).
        return _lookup_amm_pool_address(self._web3, contracts, stock_token_addr)

    def _read_pool_tokens(
        self, contracts: Contracts, pool_address: str
    ) -> tuple[str, str, int]:
        abi = contracts.pool_abis.get("univ3_pool")
        if abi is None:
            raise RuntimeError("univ3_pool ABI missing from /contracts/ payload")
        pool = self._web3.eth.contract(
            address=self._web3.to_checksum_address(pool_address), abi=abi
        )
        return (
            _call_view("UniswapV3Pool.token0", lambda: pool.functions.token0().call()),
            _call_view("UniswapV3Pool.token1", lambda: pool.functions.token1().call()),
            _call_view("UniswapV3Pool.fee", lambda: pool.functions.fee().call()),
        )

    def _map_amounts(
        self, stock_token_addr: str, token0: str, *, stock: Decimal, stablecoin: Decimal
    ) -> tuple[int, int]:
        stock_units = int(stock * _STOCK_DECIMALS)
        stablecoin_units = int(stablecoin * _STABLECOIN_DECIMALS)
        if token0.lower() == stock_token_addr.lower():
            return (stock_units, stablecoin_units)
        return (stablecoin_units, stock_units)

    def _now(self) -> int:
        return int(self._web3.eth.get_block("latest")["timestamp"])

    def _contract(self, ref: ContractRef) -> Any:
        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(ref.address), abi=ref.abi
        )

    def _approve_at(self, token_address: str, spender: str, amount: int) -> None:
        token = self._web3.eth.contract(
            address=self._web3.to_checksum_address(token_address),
            abi=_require_pool_abi(self._contracts_provider(), "erc20"),
        )
        self._send_tx(
            token.functions.approve(self._web3.to_checksum_address(spender), amount)
        )


_DUSD_EXP = 6
_STOCK_EXP = 18


class _QuoteHandler:
    def __init__(self, web3: Web3, contracts_provider: Callable[[], Contracts]) -> None:
        self._web3 = web3
        self._contracts_provider = contracts_provider

    def quote_swap(
        self, symbol: str, side: SwapSide, amount: Decimal, exact: str = "input"
    ) -> Decimal:
        contracts = self._contracts_provider()
        quoter_ref = contracts.core.quoter
        if quoter_ref is None:
            raise QuoterNotConfigured()
        stock = _resolve_stock_token(self._web3, contracts, symbol)
        dusd = contracts.core.stablecoin.address
        pool = self._contract(
            self._amm_pool(contracts, stock), self._pool_abi(contracts)
        )
        fee = _call_view("UniswapV3Pool.fee", lambda: pool.functions.fee().call())
        quoter = self._contract(quoter_ref.address, quoter_ref.abi)

        if side == SwapSide.STABLECOIN_TO_STOCK:
            token_in, token_out, in_exp, out_exp = dusd, stock, _DUSD_EXP, _STOCK_EXP
        else:
            token_in, token_out, in_exp, out_exp = stock, dusd, _STOCK_EXP, _DUSD_EXP
        token_in = self._web3.to_checksum_address(token_in)
        token_out = self._web3.to_checksum_address(token_out)

        if exact == "input":
            amount_in = int(amount * Decimal(10) ** in_exp)
            out_units = _call_view(
                "Quoter.quoteExactInputSingle",
                lambda: quoter.functions.quoteExactInputSingle(
                    token_in, token_out, fee, amount_in, 0
                ).call(),
            )
            return Decimal(out_units) / Decimal(10) ** out_exp
        if exact == "output":
            amount_out = int(amount * Decimal(10) ** out_exp)
            in_units = _call_view(
                "Quoter.quoteExactOutputSingle",
                lambda: quoter.functions.quoteExactOutputSingle(
                    token_in, token_out, fee, amount_out, 0
                ).call(),
            )
            return Decimal(in_units) / Decimal(10) ** in_exp
        raise ValueError("exact must be 'input' or 'output'")

    def spot_price(self, symbol: str) -> Decimal:
        contracts = self._contracts_provider()
        stock = _resolve_stock_token(self._web3, contracts, symbol)
        pool = self._contract(
            self._amm_pool(contracts, stock), self._pool_abi(contracts)
        )
        sqrt_price = _call_view(
            "UniswapV3Pool.slot0", lambda: pool.functions.slot0().call()
        )[0]
        token0 = _call_view(
            "UniswapV3Pool.token0", lambda: pool.functions.token0().call()
        )
        raw = (Decimal(sqrt_price) / Decimal(2**96)) ** 2
        scale = Decimal(10) ** (_STOCK_EXP - _DUSD_EXP)
        if token0.lower() == stock.lower():
            return raw * scale
        return (Decimal(1) / raw) * scale

    def pool_fee(self, symbol: str) -> int:
        contracts = self._contracts_provider()
        stock = _resolve_stock_token(self._web3, contracts, symbol)
        pool = self._contract(
            self._amm_pool(contracts, stock), self._pool_abi(contracts)
        )
        return _call_view("UniswapV3Pool.fee", lambda: pool.functions.fee().call())

    def _amm_pool(self, contracts: Contracts, stock_token_addr: str) -> str:
        return _lookup_amm_pool_address(self._web3, contracts, stock_token_addr)

    def _pool_abi(self, contracts: Contracts) -> list[Any]:
        return _require_pool_abi(contracts, "univ3_pool")

    def _contract(self, address: str, abi: Any) -> Any:
        return self._web3.eth.contract(
            address=self._web3.to_checksum_address(address), abi=abi
        )
