from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest
import requests
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import (
    ContractLogicError,
    RequestTimedOut,
    TimeExhausted,
    TransactionNotFound,
    Web3RPCError,
)

from primedelta import (
    CannotCraft,
    MarketClosed,
    NotEnoughGas,
    PrimeDelta,
    StuckTransaction,
    StuckTransactionMined,
    TradingHalted,
    TransactionFailed,
    TransactionPending,
)
from primedelta.primedelta import _READ_RETRIES
from primedelta.signer import _gas_limit_from_estimate

ADDRESS = Web3.to_checksum_address("0x" + "ab" * 20)
RECIPIENT = Web3.to_checksum_address("0x" + "cd" * 20)
GAS_PRICE = 1000 * 10**9
HASHES = [HexBytes("0x" + f"{i:02x}" * 32) for i in range(1, 5)]
DEPOSIT = "0xd0e30db0"
STALE_PRICE = "0x19abf40e"


def _revert(data: str) -> ContractLogicError:
    return ContractLogicError("execution reverted", data=data)


def _block_not_found() -> ContractLogicError:
    return ContractLogicError(
        "execution reverted: Block not found",
        data={"code": "ErrUpstreamsExhausted"},
    )


def _node_error(message: str, kind: type = Web3RPCError) -> Web3RPCError:
    error = {
        "code": -32000,
        "message": message,
        "data": {
            "code": "ErrUpstreamsExhausted",
            "message": "all upstream attempts failed (1 upstream server errors)",
        },
    }
    return kind(repr(error), rpc_response={"jsonrpc": "2.0", "id": 1, "error": error})


class _Chain:
    def __init__(self, pd: PrimeDelta, latest: int = 5, pending: int = 5) -> None:
        self.latest = latest
        self.pending = pending
        self.counts: list[object] = []
        self.balance_reads: list[object] = []
        self.balance = 10**21
        eth = pd._web3.eth
        eth.get_transaction_count.side_effect = self._count
        eth.get_balance.side_effect = self._balance

    def _count(self, address: str, block: object) -> int:
        assert address == ADDRESS
        self.counts.append(block)
        return self.pending if block == "pending" else self.latest

    def _balance(self, address: str, block: object) -> int:
        assert address == ADDRESS
        self.balance_reads.append(block)
        return self.balance


def _pd(*, wallet: bool = False, **kwargs: object) -> tuple[PrimeDelta, _Chain]:
    with patch("primedelta.primedelta.Web3"):
        pd = PrimeDelta(
            private_key="0x" + "1" * 64,
            web3_provider_url="http://x",
            network="dev",
            **kwargs,
        )
    pd._chain_verified = True
    pd._web3 = MagicMock()
    eth = pd._web3.eth
    pd._web3.to_checksum_address.side_effect = Web3.to_checksum_address
    eth.contract.side_effect = lambda address, abi: Web3().eth.contract(
        address=address, abi=abi
    )
    eth.chain_id = 2028
    eth.gas_price = GAS_PRICE
    eth.block_number = 150
    eth.estimate_gas.return_value = 40_000
    eth.wait_for_transaction_receipt.return_value = {"status": 1, "blockNumber": 300}
    pd._try_debug_trace_call = MagicMock(return_value=None)
    pd._signer = MagicMock()
    pd._signer.address = ADDRESS
    pd._signer.fills_gas_and_nonce = wallet
    pd._signer.submit_transaction.side_effect = list(HASHES)
    return pd, _Chain(pd)


def _wdel(pd: PrimeDelta) -> str:
    return pd._get_contracts().core.wdel.address


def _sent(pd: PrimeDelta) -> list[dict]:
    return [c.args[1] for c in pd._signer.submit_transaction.call_args_list]


class TestGasLimitFromEstimate:
    @pytest.mark.parametrize(
        "estimate, limit",
        [
            (21_000, 21_000),
            (21_001, 26_252),
            (40_000, 50_000),
            (52_825, 66_032),
        ],
    )
    def test_adds_a_quarter_rounded_up_except_a_plain_transfer(self, estimate, limit):
        assert _gas_limit_from_estimate(estimate) == limit


class TestEstimatedGas:
    def test_a_key_signed_send_carries_the_estimate_plus_a_quarter(self):
        pd, chain = _pd()
        assert pd.wrap_del(Decimal("0.01")) == "0x" + HASHES[0].hex()
        [tx] = _sent(pd)
        assert tx == {
            "from": ADDRESS,
            "to": _wdel(pd),
            "value": 10**16,
            "data": DEPOSIT,
            "chainId": 2028,
            "gas": 50_000,
            "gasPrice": GAS_PRICE,
            "nonce": 5,
        }
        pd._web3.eth.estimate_gas.assert_called_once_with(
            {"from": ADDRESS, "to": _wdel(pd), "value": 10**16, "data": DEPOSIT},
            "latest",
        )

    def test_estimate_balance_and_nonce_are_read_at_the_last_write(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        pd.wrap_del(Decimal("0.01"))
        assert pd._web3.eth.estimate_gas.call_args.args[1] == 200
        assert chain.balance_reads == [200]
        assert chain.counts == [200, "pending"]

    def test_a_plain_transfer_keeps_21000_gas(self):
        pd, chain = _pd()
        pd._web3.eth.estimate_gas.return_value = 21_000
        pd.send_del(RECIPIENT, Decimal("1"))
        [tx] = _sent(pd)
        assert (tx["gas"], tx["to"], tx["value"]) == (21_000, RECIPIENT, 10**18)
        assert pd._web3.eth.estimate_gas.call_args.args[0] == {
            "from": ADDRESS,
            "to": RECIPIENT,
            "value": 10**18,
            "data": "0x",
        }

    def test_a_revert_in_the_estimate_is_raised_before_signing(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        pd._web3.eth.estimate_gas.side_effect = _revert(STALE_PRICE)
        with patch("time.sleep") as sleep:
            with pytest.raises(MarketClosed) as info:
                pd.wrap_del(Decimal("0.01"))
        assert info.value.tx_hash is None
        assert pd._web3.eth.estimate_gas.call_count == 1
        sleep.assert_not_called()
        pd._signer.submit_transaction.assert_not_called()

    def test_a_lagging_replica_is_retried_not_reported_as_a_revert(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        pd._web3.eth.estimate_gas.side_effect = [_block_not_found(), 40_000]
        with patch("time.sleep"):
            pd.wrap_del(Decimal("0.01"))
        assert [c.args[1] for c in pd._web3.eth.estimate_gas.mock_calls] == [200, 200]
        assert _sent(pd)[0]["gas"] == 50_000

    def test_a_replica_that_answers_null_for_the_balance_is_retried(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        pd._web3.eth.get_balance.side_effect = [None, 10**21]
        with patch("time.sleep"):
            pd.wrap_del(Decimal("0.01"))
        assert len(_sent(pd)) == 1

    def test_an_estimate_that_never_succeeds_fails_without_sending(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        pd._web3.eth.estimate_gas.side_effect = _block_not_found()
        with patch("time.sleep"):
            with pytest.raises(TransactionFailed) as info:
                pd.wrap_del(Decimal("0.01"))
        assert not isinstance(info.value, MarketClosed)
        assert info.value.reason.startswith("could not estimate gas: ")
        assert "Nothing was signed or sent" in str(info.value)
        blocks = [c.args[1] for c in pd._web3.eth.estimate_gas.mock_calls]
        assert blocks == [200] * _READ_RETRIES + ["latest"]
        pd._signer.submit_transaction.assert_not_called()

    def test_a_balance_equal_to_the_value_is_estimated_as_is(self):
        pd, chain = _pd()
        chain.balance = 10**16
        with pytest.raises(NotEnoughGas):
            pd.wrap_del(Decimal("0.01"))
        assert len(pd._web3.eth.estimate_gas.call_args.args) == 2

    def test_a_call_that_cannot_be_encoded_is_not_sent(self):
        pd, chain = _pd()
        fn = MagicMock()
        fn.fn_name = "swap"
        fn.address = RECIPIENT
        fn._encode_transaction_data.side_effect = ValueError("bad args")
        fn.w3.eth.contract.side_effect = ValueError("bad abi")
        with pytest.raises(TransactionFailed, match="could not encode calldata"):
            pd._build_and_send_transaction(fn)
        pd._signer.submit_transaction.assert_not_called()

    def test_the_nonce_stays_ahead_of_a_lagging_pending_count(self):
        pd, chain = _pd()
        pd._next_nonce = 7
        pd._sent_nonces = {5, 6, 7}
        pd.wrap_del(Decimal("0.01"))
        assert _sent(pd)[0]["nonce"] == 8

    def test_a_mined_but_reverted_transfer_raises_with_its_hash(self):
        pd, chain = _pd()
        pd._web3.eth.estimate_gas.return_value = 30_000
        pd._web3.eth.wait_for_transaction_receipt.return_value = {
            "status": 0,
            "blockNumber": 300,
            "gasUsed": 30_000,
        }
        with pytest.raises(TransactionFailed) as info:
            pd.send_del(RECIPIENT, Decimal("1"))
        assert (info.value.tx_hash, info.value.reason) == (
            "0x" + HASHES[0].hex(),
            "reverted",
        )
        assert pd._last_write_block == 0

    def test_a_wallet_signer_fills_its_own_gas(self):
        pd, chain = _pd(wallet=True)
        pd.wrap_del(Decimal("0.01"))
        [tx] = _sent(pd)
        assert "gas" not in tx and "nonce" not in tx
        pd._web3.eth.estimate_gas.assert_not_called()
        assert chain.counts == [] and chain.balance_reads == []

    def test_craft_neither_estimates_nor_checks_the_balance(self):
        pd, chain = _pd()
        [tx] = pd.craft(lambda: pd.wrap_del(Decimal("0.01")))
        assert tx["value"] == 10**16
        pd._web3.eth.estimate_gas.assert_not_called()
        assert chain.counts == [] and chain.balance_reads == []

    def test_a_failed_broadcast_releases_the_nonce(self):
        pd, chain = _pd()
        pd._signer.submit_transaction.side_effect = ValueError("upstream timeout")
        with pytest.raises(ValueError):
            pd.wrap_del(Decimal("0.01"))
        assert pd._next_nonce is None
        pd._signer.submit_transaction.side_effect = list(HASHES)
        pd.wrap_del(Decimal("0.01"))
        assert _sent(pd)[-1]["nonce"] == 5

    @pytest.mark.parametrize(
        "error",
        [
            requests.ReadTimeout("read timed out"),
            requests.ConnectionError("connection reset by peer"),
            requests.HTTPError("504 Server Error: Gateway Timeout"),
            _node_error("Known transaction"),
            _node_error("already known"),
            _node_error("request timed out", RequestTimedOut),
            _node_error("remote endpoint request timeout"),
        ],
    )
    def test_a_broadcast_that_may_have_reached_the_pool_is_still_ours(self, error):
        pd, chain = _pd()
        pd._signer.submit_transaction.side_effect = error
        with pytest.raises(type(error)):
            pd.wrap_del(Decimal("0.01"))
        assert pd._sent_nonces == {5}
        chain.pending = 6
        pd._signer.submit_transaction.side_effect = list(HASHES)
        with patch("time.sleep") as sleep:
            pd.wrap_del(Decimal("0.01"))
        sleep.assert_not_called()
        assert _sent(pd)[-1]["nonce"] == 6

    @pytest.mark.parametrize(
        "message",
        [
            "Replacement transaction underpriced",
            "Nonce too low",
            "maxPriorityFeePerGas 500000000000 wei is below the required minimum "
            "1000000000000 wei",
            "Upfront cost exceeds account balance",
        ],
    )
    def test_a_broadcast_the_node_refused_is_not_ours(self, message):
        pd, chain = _pd()
        pd._signer.submit_transaction.side_effect = _node_error(message)
        with pytest.raises(Web3RPCError):
            pd.wrap_del(Decimal("0.01"))
        assert (pd._sent_nonces, pd._next_nonce) == (set(), None)

    def test_a_balance_read_the_node_cannot_serve_names_the_cause(self):
        pd, chain = _pd()
        pd._web3.eth.get_balance.side_effect = None
        pd._web3.eth.get_balance.return_value = None
        with pytest.raises(TransactionFailed) as info:
            pd.wrap_del(Decimal("0.01"))
        assert info.value.reason == (
            "could not estimate gas: the RPC node has not reached the requested block"
        )
        pd._signer.submit_transaction.assert_not_called()

    def test_a_revert_reported_as_an_object_is_still_a_revert(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        pd._web3.eth.estimate_gas.side_effect = ContractLogicError(
            "execution reverted: reverted with reason string 'no'",
            data={"message": "reverted", "data": "0x08c379a0"},
        )
        with patch("time.sleep") as sleep:
            with pytest.raises(TransactionFailed) as info:
                pd.wrap_del(Decimal("0.01"))
        assert "could not estimate gas" not in info.value.reason
        assert pd._web3.eth.estimate_gas.call_count == 1
        sleep.assert_not_called()


class TestNotEnoughGas:
    REQUIRED = 50_000 * GAS_PRICE + 10**16

    def test_a_balance_that_exactly_covers_gas_and_value_sends(self):
        pd, chain = _pd()
        chain.balance = self.REQUIRED
        pd.wrap_del(Decimal("0.01"))
        assert len(_sent(pd)) == 1

    def test_one_wei_short_raises_before_signing(self):
        pd, chain = _pd()
        chain.balance = self.REQUIRED - 1
        with pytest.raises(NotEnoughGas) as info:
            pd.wrap_del(Decimal("0.01"))
        error = info.value
        assert isinstance(error, TransactionFailed)
        assert error.address == ADDRESS
        assert error.balance == Decimal("0.059999999999999999")
        assert error.required == Decimal("0.06")
        assert error.shortfall == Decimal("0.000000000000000001")
        assert (error.gas_limit, error.gas_price) == (50_000, GAS_PRICE)
        assert error.value == Decimal("0.01")
        assert error.tx_hash is None
        assert str(error) == (
            f"not enough DEL for gas: {ADDRESS} holds 0.059999999999999999 DEL but "
            "deposit() needs 0.06 DEL (gas limit 50000 at 1000 gwei plus the 0.01 "
            "DEL it sends). Send at least 0.000000000000000001 DEL to "
            f"{ADDRESS}, then retry. Nothing was signed or sent."
        )
        pd._signer.submit_transaction.assert_not_called()
        assert pd._next_nonce is None

    def test_an_empty_wallet_sizes_the_gas_as_if_it_held_the_value(self):
        pd, chain = _pd()
        chain.balance = 0
        pd._web3.eth.estimate_gas.return_value = 52_825
        with pytest.raises(NotEnoughGas) as info:
            pd.wrap_del(Decimal("0.01"))
        assert pd._web3.eth.estimate_gas.call_args.args[2] == {
            ADDRESS: {"balance": 10**16}
        }
        assert info.value.gas_limit == 66_032
        assert info.value.shortfall == Decimal("0.076032")
        pd._signer.submit_transaction.assert_not_called()

    def test_send_del_from_an_empty_wallet_names_the_shortfall(self):
        pd, chain = _pd()
        chain.balance = 0
        pd._web3.eth.estimate_gas.return_value = 21_000
        with pytest.raises(NotEnoughGas) as info:
            pd.send_del(RECIPIENT, Decimal("1"))
        assert info.value.function_name == "transfer"
        assert info.value.shortfall == Decimal("1.021")
        assert "plus the 1 DEL it sends" in str(info.value)
        assert f"Send at least 1.021 DEL to {ADDRESS}" in str(info.value)
        pd._signer.submit_transaction.assert_not_called()

    def test_a_node_without_state_overrides_still_reports_the_shortfall(self):
        pd, chain = _pd()
        chain.balance = 10**15
        pd._web3.eth.estimate_gas.side_effect = ValueError("invalid params")
        with pytest.raises(NotEnoughGas) as info:
            pd.send_del(RECIPIENT, Decimal("1"))
        assert info.value.gas_limit == 21_000
        assert info.value.required == Decimal("1.021")

    def test_a_transient_error_on_the_sized_estimate_is_retried(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        chain.balance = 0
        pd._web3.eth.estimate_gas.side_effect = [_block_not_found(), 52_825]
        with patch("time.sleep"):
            with pytest.raises(NotEnoughGas) as info:
                pd.wrap_del(Decimal("0.01"))
        assert info.value.gas_limit == 66_032

    def test_a_revert_while_short_of_the_value_is_still_a_revert(self):
        pd, chain = _pd()
        chain.balance = 0
        pd._web3.eth.estimate_gas.side_effect = _revert("0xdeadbeef")
        with pytest.raises(TransactionFailed) as info:
            pd.wrap_del(Decimal("0.01"))
        assert not isinstance(info.value, NotEnoughGas)
        assert info.value.reason == "raw_data=0xdeadbeef"

    def test_message_without_a_value_leaves_the_value_out(self):
        error = NotEnoughGas("approve", ADDRESS, 0, 50_000, GAS_PRICE)
        assert "it sends" not in str(error)
        assert error.value == Decimal(0)
        assert error.shortfall == Decimal("0.05")


class TestStuckTransaction:
    def test_a_foreign_unmined_nonce_raises_after_rereading(self):
        pd, chain = _pd()
        chain.pending = 6
        with patch("time.sleep") as sleep:
            with pytest.raises(StuckTransaction) as info:
                pd.wrap_del(Decimal("0.01"))
        error = info.value
        assert isinstance(error, TransactionFailed)
        assert (error.address, error.latest_nonce, error.pending_nonce) == (
            ADDRESS,
            5,
            6,
        )
        assert sleep.call_args_list == [call(1.0), call(2.0), call(4.0), call(8.0)]
        assert "latest nonce 5, pending nonce 6" in str(error)
        assert "clear_stuck_transactions()" in str(error)
        assert "adding DEL does not clear it" in str(error)
        pd._web3.eth.estimate_gas.assert_not_called()
        pd._signer.submit_transaction.assert_not_called()

    def test_a_gap_that_closes_on_reread_is_not_stuck(self):
        pd, chain = _pd()
        chain.pending = 6
        reads = iter([5, 6])
        pd._web3.eth.get_transaction_count.side_effect = lambda address, block: (
            6 if block == "pending" else next(reads)
        )
        with patch("time.sleep") as sleep:
            pd.wrap_del(Decimal("0.01"))
        assert sleep.call_args_list == [call(1.0)]
        assert _sent(pd)[0]["nonce"] == 6

    def test_our_own_unmined_transaction_explains_the_gap(self):
        pd, chain = _pd()
        pd._sent_nonces = {5}
        chain.pending = 6
        with patch("time.sleep") as sleep:
            pd.wrap_del(Decimal("0.01"))
        sleep.assert_not_called()
        assert _sent(pd)[0]["nonce"] == 6

    def test_sequential_sends_on_a_lagging_replica_never_trip_it(self):
        pd, chain = _pd()
        pd.approve("dUSD", RECIPIENT, Decimal("1"))
        chain.pending = 6
        with patch("time.sleep") as sleep:
            pd.wrap_del(Decimal("0.01"))
        sleep.assert_not_called()
        assert [tx["nonce"] for tx in _sent(pd)] == [5, 6]
        assert chain.counts[2] == 300

    def test_each_reread_pins_to_a_newer_block(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        heights = iter([200, 210])
        type(pd._web3.eth).block_number = property(lambda self: next(heights))
        pd._web3.eth.get_transaction_count.side_effect = lambda address, block: (
            6 if block in ("pending", 210) else 5
        )
        with patch("time.sleep"):
            pd.wrap_del(Decimal("0.01"))
        assert _sent(pd)[0]["nonce"] == 6
        assert chain.balance_reads == [210]
        assert [c.args[1] for c in pd._web3.eth.estimate_gas.mock_calls] == [210]

    def test_a_reread_checks_the_balance_left_after_the_gap_mined(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        heights = iter([200, 210])
        type(pd._web3.eth).block_number = property(lambda self: next(heights))
        pd._web3.eth.get_transaction_count.side_effect = lambda address, block: (
            6 if block in ("pending", 210) else 5
        )
        balances = {200: 10**17, 210: 2 * 10**16}
        pd._web3.eth.get_balance.side_effect = lambda address, block: balances[block]
        with patch("time.sleep"):
            with pytest.raises(NotEnoughGas) as info:
                pd.wrap_del(Decimal("0.01"))
        assert info.value.balance == Decimal("0.02")
        assert [c.args[1] for c in pd._web3.eth.estimate_gas.mock_calls] == [210]
        pd._signer.submit_transaction.assert_not_called()

    def test_the_next_send_does_not_queue_behind_the_one_that_refused_ours(self):
        pd, chain = _pd()
        pd._signer.submit_transaction.side_effect = _node_error(
            "Replacement transaction underpriced"
        )
        with pytest.raises(Web3RPCError):
            pd.wrap_del(Decimal("0.01"))
        chain.pending = 6
        pd._signer.submit_transaction.side_effect = list(HASHES)
        with patch("time.sleep"):
            with pytest.raises(StuckTransaction):
                pd.wrap_del(Decimal("0.01"))
        assert len(_sent(pd)) == 1

    def test_a_refused_resend_leaves_an_earlier_transaction_ours(self):
        pd, chain = _pd()
        pd._sent_nonces = {5}
        pd._signer.submit_transaction.side_effect = _node_error(
            "Replacement transaction underpriced"
        )
        with pytest.raises(Web3RPCError):
            pd.wrap_del(Decimal("0.01"))
        assert pd._sent_nonces == {5}
        chain.pending = 6
        pd._signer.submit_transaction.side_effect = list(HASHES)
        with patch("time.sleep") as sleep:
            pd.wrap_del(Decimal("0.01"))
        sleep.assert_not_called()
        assert _sent(pd)[-1]["nonce"] == 6

    def test_nonces_below_the_latest_are_forgotten(self):
        pd, chain = _pd()
        pd._sent_nonces = {3, 4, 5}
        chain.pending = 6
        pd.wrap_del(Decimal("0.01"))
        assert pd._sent_nonces == {5, 6}

    def test_the_latest_nonce_is_retried_when_the_replica_has_no_block(self):
        pd, chain = _pd()
        pd._last_write_block = 200
        answers = iter([None, 5])
        pd._web3.eth.get_transaction_count.side_effect = lambda address, block: (
            5 if block == "pending" else next(answers)
        )
        with patch("time.sleep"):
            pd.wrap_del(Decimal("0.01"))
        assert _sent(pd)[0]["nonce"] == 5


class TestTransactionPending:
    def test_a_receipt_that_never_comes_raises_with_the_hash(self):
        pd, chain = _pd(receipt_timeout=12.5)
        pd._web3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted()
        with pytest.raises(TransactionPending) as info:
            pd.wrap_del(Decimal("0.01"))
        error = info.value
        assert isinstance(error, TransactionFailed)
        assert error.tx_hash == "0x" + HASHES[0].hex()
        assert (error.function_name, error.timeout) == ("deposit", 12.5)
        assert str(error).startswith(
            f"deposit() transaction 0x{HASHES[0].hex()} was sent but did not mine "
            "within 12.5 s. It may still mine"
        )
        assert "whose next send raises StuckTransaction until then" in str(error)
        assert "clear_stuck_transactions()" in str(error)
        pd._web3.eth.wait_for_transaction_receipt.assert_called_once_with(
            HASHES[0], timeout=12.5, poll_latency=0.5
        )

    def test_the_default_wait_is_explicit(self):
        pd, chain = _pd()
        pd.wrap_del(Decimal("0.01"))
        assert pd._web3.eth.wait_for_transaction_receipt.call_args.kwargs == {
            "timeout": 60.0,
            "poll_latency": 0.5,
        }

    def test_the_next_send_does_not_queue_behind_a_timed_out_one(self):
        pd, chain = _pd()
        pd._web3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted()
        with pytest.raises(TransactionPending):
            pd.wrap_del(Decimal("0.01"))
        assert (pd._sent_nonces, pd._next_nonce) == (set(), None)
        chain.pending = 6
        with patch("time.sleep"):
            with pytest.raises(StuckTransaction):
                pd.wrap_del(Decimal("0.01"))
        assert len(_sent(pd)) == 1

    def test_a_timed_out_one_that_mines_while_rereading_lets_the_next_through(self):
        pd, chain = _pd()
        pd._web3.eth.wait_for_transaction_receipt.side_effect = [
            TimeExhausted(),
            {"status": 1, "blockNumber": 300},
        ]
        with pytest.raises(TransactionPending):
            pd.wrap_del(Decimal("0.01"))
        chain.pending = 6

        def mined(delay):
            chain.latest = 6

        with patch("time.sleep", side_effect=mined) as sleep:
            pd.wrap_del(Decimal("0.01"))
        assert sleep.call_args_list == [call(1.0)]
        assert [tx["nonce"] for tx in _sent(pd)] == [5, 6]

    @pytest.mark.parametrize("wallet", [False, True])
    def test_a_value_transfer_raises_it_too(self, wallet):
        pd, chain = _pd(wallet=wallet)
        pd._web3.eth.estimate_gas.return_value = 21_000
        pd._web3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted()
        with pytest.raises(TransactionPending) as info:
            pd.send_del(RECIPIENT, Decimal("1"))
        assert (info.value.function_name, info.value.to) == ("transfer", RECIPIENT)


class TestOutOfGas:
    def _mined_failed(self, pd: PrimeDelta, gas_used: int) -> None:
        pd._web3.eth.wait_for_transaction_receipt.return_value = {
            "status": 0,
            "blockNumber": 300,
            "gasUsed": gas_used,
        }
        pd._web3.eth.call.return_value = b""

    @pytest.mark.parametrize(
        "gas_used, reason",
        [
            (
                50_000,
                "ran out of gas: it used all 50000 gas of its estimated limit, most "
                "likely because the state changed between the estimate and its "
                "block. Nothing it did took effect, but the gas was paid; a retry "
                "estimates the gas again",
            ),
            (49_999, "reverted with no reason"),
        ],
    )
    def test_a_call_that_used_all_its_gas_ran_out_of_it(self, gas_used, reason):
        pd, chain = _pd()
        self._mined_failed(pd, gas_used)
        with pytest.raises(TransactionFailed) as info:
            pd.wrap_del(Decimal("0.01"))
        assert type(info.value) is TransactionFailed
        assert info.value.reason == reason
        assert info.value.tx_hash == "0x" + HASHES[0].hex()
        assert str(info.value).endswith(f"reason={reason}")
        replayed = [c.args[1] for c in pd._web3.eth.call.call_args_list]
        assert replayed == ([] if gas_used == 50_000 else [299])

    @pytest.mark.parametrize("gas_used, ran_out", [(37_500, True), (37_499, False)])
    def test_a_transfer_to_a_contract_that_used_all_its_gas(self, gas_used, ran_out):
        pd, chain = _pd()
        pd._web3.eth.estimate_gas.return_value = 30_000
        self._mined_failed(pd, gas_used)
        with pytest.raises(TransactionFailed) as info:
            pd.send_del(RECIPIENT, Decimal("1"))
        assert _sent(pd)[0]["gas"] == 37_500
        assert info.value.reason.startswith(
            "ran out of gas: it used all 37500 gas" if ran_out else "reverted"
        )
        assert ("gas" in info.value.reason) is ran_out

    def test_a_wallet_that_set_its_own_gas_is_replayed(self):
        pd, chain = _pd(wallet=True)
        self._mined_failed(pd, 50_000)
        with pytest.raises(TransactionFailed) as info:
            pd.wrap_del(Decimal("0.01"))
        assert info.value.reason == "reverted with no reason"
        assert pd._web3.eth.call.call_args.args[1] == 299


class TestClearStuckTransactions:
    BUMPED = 1_250_000_002

    def _stuck(self, latest: int = 5, pending: int = 7) -> tuple[PrimeDelta, _Chain]:
        pd, chain = _pd()
        pd._web3.eth.gas_price = 10**9 + 1
        chain.latest, chain.pending = latest, pending
        return pd, chain

    def test_nothing_stuck_sends_nothing(self):
        pd, chain = self._stuck(pending=5)
        assert pd.clear_stuck_transactions() == []
        pd._signer.submit_transaction.assert_not_called()

    def test_nothing_stuck_reads_no_balance(self):
        pd, chain = self._stuck(pending=5)
        pd.clear_stuck_transactions()
        assert chain.balance_reads == []

    def test_replaces_each_stuck_nonce_with_a_self_transfer(self):
        pd, chain = self._stuck()
        pd._next_nonce = 9
        hashes = pd.clear_stuck_transactions()
        assert hashes == ["0x" + HASHES[0].hex(), "0x" + HASHES[1].hex()]
        assert _sent(pd) == [
            {
                "from": ADDRESS,
                "to": ADDRESS,
                "value": 0,
                "gas": 21_000,
                "gasPrice": self.BUMPED,
                "nonce": nonce,
                "chainId": 2028,
            }
            for nonce in (5, 6)
        ]
        assert pd._web3.eth.wait_for_transaction_receipt.call_args_list == [
            call(HASHES[0], timeout=60.0, poll_latency=0.5),
            call(HASHES[1], timeout=60.0, poll_latency=0.5),
        ]
        assert pd._next_nonce is None
        assert pd._last_write_block == 300
        assert pd._sent_nonces == {5, 6}

    def test_reads_are_pinned_to_the_last_write(self):
        pd, chain = self._stuck()
        pd._last_write_block = 200
        pd.clear_stuck_transactions()
        assert chain.counts == [200, "pending"]
        assert chain.balance_reads == [200]

    def test_a_balance_that_exactly_covers_every_replacement_sends(self):
        pd, chain = self._stuck()
        chain.balance = 2 * 21_000 * self.BUMPED
        assert len(pd.clear_stuck_transactions()) == 2

    def test_one_wei_short_raises_before_sending(self):
        pd, chain = self._stuck()
        chain.balance = 2 * 21_000 * self.BUMPED - 1
        with pytest.raises(NotEnoughGas) as info:
            pd.clear_stuck_transactions()
        assert info.value.function_name == "clear_stuck_transactions"
        assert (info.value.gas_limit, info.value.gas_price) == (42_000, self.BUMPED)
        assert info.value.shortfall == Decimal("0.000000000000000001")
        pd._signer.submit_transaction.assert_not_called()

    def test_a_replacement_that_does_not_mine_raises_pending(self):
        pd, chain = self._stuck()
        pd._web3.eth.wait_for_transaction_receipt.side_effect = TimeExhausted()
        with pytest.raises(TransactionPending) as info:
            pd.clear_stuck_transactions()
        assert info.value.function_name == "clear_stuck_transactions"
        assert info.value.tx_hash == "0x" + HASHES[0].hex()

    def test_every_replacement_is_sent_before_any_receipt_is_awaited(self):
        pd, chain = self._stuck()
        events: list[tuple[str, object]] = []
        hashes = iter(HASHES)

        def submit(web3, tx):
            events.append(("submit", tx["nonce"]))
            return next(hashes)

        def wait(tx_hash, **kwargs):
            events.append(("wait", tx_hash))
            return {"status": 1, "blockNumber": 300}

        pd._signer.submit_transaction.side_effect = submit
        pd._web3.eth.wait_for_transaction_receipt.side_effect = wait
        pd.clear_stuck_transactions()
        assert events == [
            ("submit", 5),
            ("submit", 6),
            ("wait", HASHES[0]),
            ("wait", HASHES[1]),
        ]

    def test_a_nonce_already_mined_is_reported_and_the_rest_replaced(self):
        pd, chain = self._stuck(pending=8)
        pd._next_nonce = 9
        pd._signer.submit_transaction.side_effect = [
            HASHES[0],
            ValueError("{'code': -32001, 'message': 'Nonce too low'}"),
            HASHES[2],
        ]
        with pytest.raises(StuckTransactionMined) as info:
            pd.clear_stuck_transactions()
        error = info.value
        assert isinstance(error, TransactionFailed)
        assert (error.address, error.mined_nonces) == (ADDRESS, [6])
        assert error.cleared == ["0x" + HASHES[0].hex(), "0x" + HASHES[2].hex()]
        assert [tx["nonce"] for tx in _sent(pd)] == [5, 6, 7]
        waited = pd._web3.eth.wait_for_transaction_receipt.call_args_list
        assert [c.args[0] for c in waited] == [HASHES[0], HASHES[2]]
        assert str(error) == (
            f"{ADDRESS}: another transaction mined at nonce 6 before its "
            "replacement could, most likely the stuck transaction itself, so it "
            "ran instead of being cancelled: check the account's balances before "
            "acting on it. Replacements that mined: nonce 5 "
            f"0x{HASHES[0].hex()}, nonce 7 0x{HASHES[2].hex()}. Nothing is stuck "
            "at these nonces any more."
        )
        assert pd._next_nonce is None
        assert pd._last_write_block == 300

    def test_a_replacement_beaten_to_its_nonce_is_reported(self):
        pd, chain = self._stuck()

        def wait(tx_hash, **kwargs):
            if tx_hash == HASHES[1]:
                chain.latest = 7
                raise TimeExhausted()
            return {"status": 1, "blockNumber": 300}

        pd._web3.eth.wait_for_transaction_receipt.side_effect = wait
        pd._web3.eth.get_transaction_receipt.side_effect = TransactionNotFound("no")
        with pytest.raises(StuckTransactionMined) as info:
            pd.clear_stuck_transactions()
        assert info.value.mined_nonces == [6]
        assert info.value.cleared == ["0x" + HASHES[0].hex()]
        assert "No replacement" not in str(info.value)
        pd._web3.eth.get_transaction_receipt.assert_called_once_with(HASHES[1])
        assert chain.counts[-1] == 300

    def test_only_a_beaten_replacement_says_none_mined(self):
        pd, chain = self._stuck(pending=6)

        def wait(tx_hash, **kwargs):
            chain.latest = 6
            raise TimeExhausted()

        pd._web3.eth.wait_for_transaction_receipt.side_effect = wait
        pd._web3.eth.get_transaction_receipt.side_effect = TransactionNotFound("no")
        with pytest.raises(StuckTransactionMined) as info:
            pd.clear_stuck_transactions()
        assert (info.value.mined_nonces, info.value.cleared) == ([5], [])
        assert "No replacement mined." in str(info.value)

    def test_a_replacement_that_mined_just_after_the_wait_counts(self):
        pd, chain = self._stuck()

        def wait(tx_hash, **kwargs):
            if tx_hash == HASHES[1]:
                chain.latest = 7
                raise TimeExhausted()
            return {"status": 1, "blockNumber": 300}

        pd._web3.eth.wait_for_transaction_receipt.side_effect = wait
        pd._web3.eth.get_transaction_receipt.return_value = {
            "status": 1,
            "blockNumber": 301,
        }
        hashes = pd.clear_stuck_transactions()
        assert hashes == ["0x" + HASHES[0].hex(), "0x" + HASHES[1].hex()]
        assert pd._last_write_block == 301

    def test_a_replacement_whose_nonce_is_still_open_stays_pending(self):
        pd, chain = self._stuck()

        def wait(tx_hash, **kwargs):
            if tx_hash == HASHES[1]:
                chain.latest = 6
                raise TimeExhausted()
            return {"status": 1, "blockNumber": 300}

        pd._web3.eth.wait_for_transaction_receipt.side_effect = wait
        with pytest.raises(TransactionPending) as info:
            pd.clear_stuck_transactions()
        assert info.value.tx_hash == "0x" + HASHES[1].hex()
        pd._web3.eth.get_transaction_receipt.assert_not_called()

    def test_nonces_taken_while_sending_and_waiting_are_named_in_order(self):
        pd, chain = self._stuck()
        pd._signer.submit_transaction.side_effect = [
            HASHES[0],
            ValueError("nonce too low: next nonce 7, tx nonce 6"),
        ]

        def wait(tx_hash, **kwargs):
            chain.latest = 7
            raise TimeExhausted()

        pd._web3.eth.wait_for_transaction_receipt.side_effect = wait
        pd._web3.eth.get_transaction_receipt.side_effect = TransactionNotFound("no")
        with pytest.raises(StuckTransactionMined) as info:
            pd.clear_stuck_transactions()
        assert (info.value.mined_nonces, info.value.cleared) == ([5, 6], [])

    def test_several_mined_nonces_are_named_together(self):
        error = StuckTransactionMined(
            "clear_stuck_transactions", ADDRESS, [5, 6], [(7, "0xab")]
        )
        assert str(error) == (
            f"{ADDRESS}: other transactions mined at nonces 5, 6 before their "
            "replacements could, most likely the stuck transactions themselves, so "
            "they ran instead of being cancelled: check the account's balances "
            "before acting on it. Replacements that mined: nonce 7 0xab. Nothing is "
            "stuck at these nonces any more."
        )
        assert (error.mined_nonces, error.cleared) == ([5, 6], ["0xab"])

    def test_an_underpriced_replacement_says_to_cancel_the_stuck_one(self):
        pd, chain = self._stuck()
        pd._signer.submit_transaction.side_effect = ValueError(
            "replacement transaction underpriced"
        )
        with pytest.raises(TransactionFailed) as info:
            pd.clear_stuck_transactions()
        assert info.value.reason == (
            "the node refused the replacement for nonce 5: replacement transaction "
            "underpriced"
        )
        assert str(info.value) == (
            "clear_stuck_transactions(): the node refused the replacement for nonce "
            "5: replacement transaction underpriced. The transaction stuck at nonce 5 "
            "may pay more than this replacement's 1.250000002 gwei (25% above the "
            "current gas price), and a node only replaces a transaction with one "
            "that pays at least 10% more. Calling clear_stuck_transactions() again "
            "signs the same replacement: cancel or speed up the stuck transaction at "
            "a higher gas price from the wallet or app that sent it."
        )

    @pytest.mark.parametrize(
        "error, waits",
        [
            (_node_error("Replacement transaction underpriced"), False),
            (_node_error("REPLACEMENT_UNDERPRICED"), False),
            (_node_error("already known"), True),
            (_node_error("Known transaction"), True),
            (_node_error("request timed out", RequestTimedOut), True),
            (requests.ConnectionError("connection reset by peer"), True),
            (_node_error("Upfront cost exceeds account balance"), False),
        ],
    )
    def test_only_a_replacement_that_may_be_pending_says_to_wait(self, error, waits):
        pd, chain = self._stuck()
        pd._signer.submit_transaction.side_effect = error
        with pytest.raises(TransactionFailed) as info:
            pd.clear_stuck_transactions()
        message = str(info.value)
        assert info.value.reason == (
            f"the node refused the replacement for nonce 5: {error}"
        )
        assert message.startswith(f"clear_stuck_transactions(): {info.value.reason}.")
        assert (
            "may already be pending: wait for it to mine, then call "
            "clear_stuck_transactions() again." in message
        ) is waits
        assert ("from the wallet or app that sent it" in message) is (
            "underpriced" in str(error).lower()
        )

    def test_a_refused_replacement_leaves_its_nonce_stuck(self):
        pd, chain = self._stuck()
        pd._signer.submit_transaction.side_effect = [
            HASHES[0],
            _node_error("Replacement transaction underpriced"),
        ]
        with pytest.raises(TransactionFailed):
            pd.clear_stuck_transactions()
        assert pd._sent_nonces == {5}
        pd._signer.submit_transaction.side_effect = list(HASHES)
        with patch("time.sleep"):
            with pytest.raises(StuckTransaction) as info:
                pd.wrap_del(Decimal("0.01"))
        assert (info.value.latest_nonce, info.value.pending_nonce) == (5, 7)
        assert [tx["nonce"] for tx in _sent(pd)] == [5, 6]

    def test_a_wallet_signer_is_refused(self):
        pd, chain = _pd(wallet=True)
        with pytest.raises(RuntimeError, match="needs a key or KMS signer"):
            pd.clear_stuck_transactions()
        pd._signer.submit_transaction.assert_not_called()

    def test_craft_is_refused(self):
        pd, chain = self._stuck()
        with pytest.raises(CannotCraft):
            pd.craft(pd.clear_stuck_transactions)
        pd._signer.submit_transaction.assert_not_called()

    def test_the_kill_switch_holds(self):
        pd, chain = self._stuck()
        pd.halt()
        with pytest.raises(TradingHalted):
            pd.clear_stuck_transactions()
        pd._signer.submit_transaction.assert_not_called()


class TestTransactionFailedMessage:
    def test_a_custom_message_replaces_the_revert_summary(self):
        error = TransactionFailed("swap", "why", message="custom")
        assert (str(error), error.reason) == ("custom", "why")

    def test_the_default_message_is_the_revert_summary(self):
        assert str(TransactionFailed("swap", "why")) == "swap() reverted; reason=why"
