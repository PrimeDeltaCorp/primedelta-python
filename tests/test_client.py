import dataclasses
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import requests

from primedelta import primedelta_client
from primedelta.primedelta_client import (
    APIError,
    AuthorizationError,
    BackendUnavailable,
    NotLoggedIn,
    PrimeDeltaClient,
    UserSignedMessageVerificationError,
    _TimeoutSession,
)
from primedelta.types import (
    DistributionType,
    FiatWithdrawalBankAccount,
    OrderSide,
    OrderStatus,
    TransactionType,
    TransferHistoryStatus,
)

_UNSET = object()


class _Resp:
    def __init__(self, status_code=200, json_data=_UNSET, content=None):
        self.status_code = status_code
        self._json = json_data
        if content is not None:
            self.content = content
        elif json_data is _UNSET:
            self.content = b""
        else:
            self.content = b'{"body": true}'
        self.headers: dict[str, str] = {}

    def json(self):
        if self._json is _UNSET:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _client_with_session():
    client = PrimeDeltaClient()
    session = MagicMock()
    client._session = session
    return client, session


class TestLogin:
    def test_verify_carries_csrf_origin_and_referer(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(204)

        client.login(message="m", signature="s", nonce="n")

        session.request.assert_called_once()
        method, url = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/users/verify/")
        assert kwargs["data"] == {"message": "m", "signature": "s", "nonce": "n"}
        assert kwargs["headers"] == {
            "X-CSRFToken": "tok",
            "Origin": client._origin(),
            "Referer": client._origin() + "/",
        }
        session.post.assert_not_called()
        assert client._csrf_token is None

    def test_a_relogin_with_a_stale_token_retries_once_with_a_fresh_one(self):
        client, session = _client_with_session()
        client._csrf_token = "stale"
        session.get.return_value = _Resp(200, {"csrfToken": "fresh"})
        session.request.side_effect = [
            _Resp(403, {"detail": ["CSRF Failed: CSRF token incorrect."]}),
            _Resp(204),
        ]

        client.login(message="m", signature="s", nonce="n")

        verify = client._url("/users/verify/")
        body = {"message": "m", "signature": "s", "nonce": "n"}
        sent = [
            (*call.args, call.kwargs["data"], call.kwargs["headers"]["X-CSRFToken"])
            for call in session.request.call_args_list
        ]
        assert sent == [
            ("POST", verify, body, "stale"),
            ("POST", verify, body, "fresh"),
        ]
        assert client._csrf_token is None

    def test_a_second_forbidden_login_raises(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(403, {"detail": ["CSRF Failed"]})

        with pytest.raises(requests.HTTPError):
            client.login(message="m", signature="s", nonce="n")
        assert session.request.call_count == 2

    def test_a_business_refusal_is_not_retried(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        code = next(iter(primedelta_client._BUSINESS_403_CODES))
        session.request.return_value = _Resp(403, {"errorCode": code})

        with pytest.raises(requests.HTTPError):
            client.login(message="m", signature="s", nonce="n")
        session.request.assert_called_once()

    def test_raises_on_message_verification_error(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(
            400, {"errorCode": "MESSAGE_VERIFICATION_ERROR"}
        )

        with pytest.raises(UserSignedMessageVerificationError):
            client.login(message="m", signature="bad", nonce="n")
        session.request.assert_called_once()


class TestCsrf:
    def test_unsafe_request_attaches_csrf_origin_referer_and_body(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 7})

        withdrawal_id = client.request_stablecoin_withdrawal(Decimal("10"))

        assert withdrawal_id == 7
        session.get.assert_any_call(client._url("/csrf-token/"))
        method, url = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/initialize-stablecoin-withdraw/")
        headers = kwargs["headers"]
        assert headers["X-CSRFToken"] == "tok"
        assert headers["Origin"] == client._origin()
        assert headers["Referer"] == client._origin() + "/"
        assert kwargs["json"] == {"amount": "10", "symbol": "dUSD"}

    def test_get_request_carries_no_csrf(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"items": []})

        client.claimable_withdrawals()

        session.get.assert_not_called()
        headers = session.request.call_args.kwargs["headers"]
        assert "X-CSRFToken" not in headers

    def test_csrf_token_fetched_once_and_cached(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 1})

        client.request_stablecoin_withdrawal(Decimal("1"))
        client.request_stablecoin_withdrawal(Decimal("2"))

        csrf_calls = [
            c
            for c in session.get.call_args_list
            if c.args and c.args[0].endswith("/csrf-token/")
        ]
        assert len(csrf_calls) == 1


class TestErrorMapping:
    def test_401_raises_not_logged_in(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            401, {"detail": ["x"], "code": "NOT_AUTHENTICATED"}
        )
        with pytest.raises(NotLoggedIn):
            client.me()

    def test_403_raises_authorization_error(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            403, {"detail": ["x"], "code": "PERMISSION_DENIED"}
        )
        with pytest.raises(AuthorizationError):
            client.me()

    def test_400_business_errorcode_preserved(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(400, {"errorCode": "INSUFFICIENT_FUNDS"})
        with pytest.raises(APIError) as info:
            client.portfolio()
        assert info.value.error_code == "INSUFFICIENT_FUNDS"

    def test_400_drf_shape_falls_back_to_code(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(400, {"detail": ["x"], "code": "PARSE"})
        with pytest.raises(APIError) as info:
            client.portfolio()
        assert info.value.error_code == "PARSE"

    def test_400_prefers_errorcode_over_code(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            400, {"errorCode": "PRIMARY", "code": "secondary"}
        )
        with pytest.raises(APIError) as info:
            client.portfolio()
        assert info.value.error_code == "PRIMARY"

    def test_400_keeps_backend_message(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(
            400,
            {
                "errorCode": "INVALID_QUANTITY_PRECISION",
                "message": "AAPL trades in whole shares",
            },
        )
        with pytest.raises(APIError) as info:
            client.send_sell_market_order(Decimal("0.5"), "AAPL")
        assert info.value.error_code == "INVALID_QUANTITY_PRECISION"
        assert info.value.message == "AAPL trades in whole shares"
        assert str(info.value) == (
            "INVALID_QUANTITY_PRECISION: AAPL trades in whole shares"
        )

    def test_400_keeps_drf_detail(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        detail = {"amount": ["Ensure that there are no more than 2 decimal places."]}
        session.request.return_value = _Resp(
            400, {"detail": detail, "code": "INVALID_REQUEST"}
        )
        with pytest.raises(APIError) as info:
            client.get_deposit_stablecoin_signature(Decimal("1.25"), "dUSD")
        assert info.value.error_code == "INVALID_REQUEST"
        assert info.value.message is None
        assert info.value.detail == detail
        assert "no more than 2 decimal places" in str(info.value)

    def test_error_without_message_or_detail_reads_as_the_code(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(400, {"errorCode": "INSUFFICIENT_FUNDS"})
        with pytest.raises(APIError) as info:
            client.portfolio()
        assert str(info.value) == "INSUFFICIENT_FUNDS"
        assert info.value.message is None and info.value.detail is None


class TestReads:
    def test_me_returns_address(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"address": "0xABC"})
        assert client.me() == "0xABC"

    def test_get_account_status_maps_verified_minted(self):
        from primedelta.types import AccountStatus

        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"status": "VERIFIED_MINTED"})
        assert client.get_account_status() == AccountStatus.DID_MINTED

    def test_portfolio_handles_null_profit_loss_percentage(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "balance": {
                    "available": "10",
                    "equity": "20",
                    "funds": "10",
                    "profitLoss": "0",
                    "totalValue": "30",
                },
                "stocks": [
                    {
                        "symbol": "AMMT1",
                        "name": "AMM Test 1",
                        "totalOwned": "20",
                        "availableToSell": "20",
                        "averagePurchasePrice": "0",
                        "lastMarketPrice": "10",
                        "profitLoss": "0",
                        "profitLossPercentage": None,
                        "isOffboarded": False,
                        "multiplierNumerator": 1,
                        "multiplierDenominator": 1,
                    }
                ],
            },
        )
        portfolio = client.portfolio()
        assert portfolio.positions[0].profit_loss_percentage is None

    @pytest.mark.parametrize(
        "item, quantity_decimals, price_decimals",
        [
            ({"quantityDecimals": 0, "priceDecimals": 2}, 0, 2),
            ({}, None, None),
        ],
    )
    def test_portfolio_parses_stock_precision(
        self, item, quantity_decimals, price_decimals
    ):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "balance": {
                    "available": "0",
                    "equity": "0",
                    "funds": "0",
                    "profitLoss": "0",
                    "totalValue": "0",
                },
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc",
                        "totalOwned": "3",
                        "availableToSell": "3",
                        "averagePurchasePrice": "150.25",
                        "lastMarketPrice": "151.00",
                        "profitLoss": "2.25",
                        "profitLossPercentage": "0.50",
                        "isOffboarded": False,
                        "multiplierNumerator": 1,
                        "multiplierDenominator": 1,
                        **item,
                    }
                ],
            },
        )
        [position] = client.portfolio().positions
        assert position.quantity_decimals == quantity_decimals
        assert position.price_decimals == price_decimals

    @pytest.mark.parametrize(
        "item, quantity_decimals, price_decimals",
        [
            ({"quantityDecimals": 0, "priceDecimals": 2}, 0, 2),
            ({"quantityDecimals": 6, "priceDecimals": 4}, 6, 4),
            ({}, None, None),
        ],
    )
    def test_stocks_parses_stock_precision(
        self, item, quantity_decimals, price_decimals
    ):
        client, session = _client_with_session()
        session.get.return_value = _Resp(
            200,
            {
                "items": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc",
                        "cusipId": "037833100",
                        "smartContractAddress": "0x" + "1" * 40,
                        "numberOfTokens": "1000000",
                        **item,
                    }
                ]
            },
        )
        stock = client.stocks()["AAPL"]
        assert stock.number_of_tokens_in_circulation == Decimal("1000000")
        assert stock.quantity_decimals == quantity_decimals
        assert stock.price_decimals == price_decimals

    def test_stocks_reads_every_page(self):
        client, session = _client_with_session()

        def item(symbol):
            return {
                "symbol": symbol,
                "name": symbol,
                "cusipId": "0",
                "smartContractAddress": "0x" + "1" * 40,
                "numberOfTokens": "1",
            }

        session.get.side_effect = [
            _Resp(200, {"items": [item("A"), item("B")], "total": 3, "count": 2}),
            _Resp(200, {"items": [item("C")], "total": 3, "count": 1}),
        ]
        assert sorted(client.stocks()) == ["A", "B", "C"]
        pages = [call.kwargs["params"]["page"] for call in session.get.call_args_list]
        assert pages == [1, 2]

    def test_stocks_stops_at_the_page_cap(self):
        client, session = _client_with_session()
        item = {
            "symbol": "A",
            "name": "A",
            "cusipId": "0",
            "smartContractAddress": "0x" + "1" * 40,
            "numberOfTokens": "1",
        }
        session.get.return_value = _Resp(
            200, {"items": [item], "total": 10**9, "count": 1}
        )
        client.stocks()
        assert session.get.call_count == 50

    def test_stocks_stops_on_an_empty_page(self):
        client, session = _client_with_session()
        session.get.side_effect = [
            _Resp(200, {"items": [], "total": 5, "count": 0}),
        ]
        assert client.stocks() == {}
        assert session.get.call_count == 1

    def test_touch_session_reports_a_live_session(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"status": "VERIFIED"})
        assert client.touch_session() is True

    def test_touch_session_never_relogs_in(self):
        client, session = _client_with_session()
        relogin = MagicMock()
        client.set_relogin(relogin)
        session.request.return_value = _Resp(401, {})
        assert client.touch_session() is False
        relogin.assert_not_called()
        assert session.request.call_count == 1

    def test_touch_session_types_a_server_error(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(503, {})
        with pytest.raises(BackendUnavailable):
            client.touch_session()

    def test_prices_stream_token_minted(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"token": "abc"})
        assert client.prices_stream_access_token() == "abc"


class TestAuditFixes:
    def test_account_status_accepts_new_kyc_states(self):
        from primedelta.types import AccountStatus

        client, session = _client_with_session()
        for value, expected in [
            ("ON_HOLD", AccountStatus.ON_HOLD),
            ("RESUBMISSION_REQUESTED", AccountStatus.RESUBMISSION_REQUESTED),
            ("REJECTED_FINAL", AccountStatus.REJECTED_FINAL),
            ("INVALID", AccountStatus.INVALID),
            (
                "AWAITING_MAIN_CONFIRMATION",
                AccountStatus.AWAITING_MAIN_CONFIRMATION,
            ),
            ("SUBACCOUNT_REJECTED", AccountStatus.SUBACCOUNT_REJECTED),
        ]:
            session.request.return_value = _Resp(200, {"status": value})
            assert client.get_account_status() == expected

    def test_distributions_accepts_other_type(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "items": [
                    {
                        "amount": "1",
                        "type": "OTHER",
                        "stockSymbol": "X",
                        "quantity": "1",
                    }
                ]
            },
        )
        assert client.get_distributions(1, 10)[0].type == DistributionType.OTHER

    def test_portfolio_handles_null_last_market_price(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "balance": {
                    "available": "0",
                    "equity": "0",
                    "funds": "0",
                    "profitLoss": "0",
                    "totalValue": "0",
                },
                "stocks": [
                    {
                        "symbol": "X",
                        "name": "X",
                        "totalOwned": "1",
                        "availableToSell": "1",
                        "averagePurchasePrice": "0",
                        "lastMarketPrice": None,
                        "profitLoss": "0",
                        "profitLossPercentage": None,
                        "isOffboarded": False,
                        "multiplierNumerator": 1,
                        "multiplierDenominator": 1,
                    }
                ],
            },
        )
        position = client.portfolio().positions[0]
        assert position.last_market_price is None
        assert position.profit_loss_percentage is None

    def test_404_with_error_code_maps_to_api_error(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(404, {"errorCode": "ORDER_NOT_FOUND"})
        with pytest.raises(APIError) as info:
            client.cancel_order(5)
        assert info.value.error_code == "ORDER_NOT_FOUND"

    def test_404_without_error_code_raises_http_error(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(404)
        with pytest.raises(requests.HTTPError):
            client.cancel_order(5)

    def test_stale_csrf_403_refetches_and_retries_once(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.side_effect = [
            _Resp(403, {"detail": ["csrf"], "code": "PERMISSION_DENIED"}),
            _Resp(200, {"withdrawalId": 9}),
        ]
        assert client.request_stablecoin_withdrawal(Decimal("1")) == 9
        assert session.request.call_count == 2
        csrf_gets = [
            c
            for c in session.get.call_args_list
            if c.args and c.args[0].endswith("/csrf-token/")
        ]
        assert len(csrf_gets) == 2

    def test_persistent_403_raises_after_one_retry(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.side_effect = [
            _Resp(403, {"code": "PERMISSION_DENIED"}),
            _Resp(403, {"code": "PERMISSION_DENIED"}),
        ]
        with pytest.raises(AuthorizationError):
            client.request_stablecoin_withdrawal(Decimal("1"))
        assert session.request.call_count == 2

    def test_logout_clears_session_even_when_post_fails(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(403, {"code": "PERMISSION_DENIED"})
        with pytest.raises(AuthorizationError):
            client.logout()
        assert client._csrf_token is None
        session.cookies.clear.assert_called_once()


class TestAccountFeatures:
    def test_messages_parses_items(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, [{"id": 3, "content": "hi"}])
        messages = client.messages()
        assert messages[0].id == 3 and messages[0].content == "hi"

    def test_mark_message_read_posts_to_message(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200)
        client.mark_message_read(7)
        method, url = session.request.call_args.args
        assert method == "POST" and url.endswith("/messages/7/")

    def test_bank_details_parses_nullable_fields(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "beneficiaryName": "X Ltd",
                "beneficiaryAddress": "somewhere",
                "referenceCode": "REF-1",
                "bankName": "A Bank",
                "bic": "AAAABBCC",
                "accountNumber": None,
                "transitNumber": None,
                "institutionNumber": None,
                "bankAddress": None,
            },
        )
        details = client.bank_details()
        assert details.reference_code == "REF-1"
        assert details.account_number is None

    def test_request_fiat_withdrawal_posts_bank_account_and_returns_id(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 12})
        bank_account = FiatWithdrawalBankAccount(
            beneficiary_name="Jane Doe",
            beneficiary_address="1 Main St, Toronto",
            bank_name="A Bank",
            account_number="1234567",
            transit_number="12345",
            institution_number="001",
            bic="AAAACATT",
            bank_address="2 Bay St, Toronto",
        )
        assert client.request_fiat_withdrawal(Decimal("50"), bank_account) == 12
        assert session.request.call_args.kwargs["json"] == {
            "amount": "50",
            "beneficiaryName": "Jane Doe",
            "beneficiaryAddress": "1 Main St, Toronto",
            "bankName": "A Bank",
            "accountNumber": "1234567",
            "transitNumber": "12345",
            "institutionNumber": "001",
            "bic": "AAAACATT",
            "bankAddress": "2 Bay St, Toronto",
        }

    def test_request_fiat_withdrawal_surfaces_invalid_request_code(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(
            400, {"errorCode": "INVALID_WITHDRAWAL_REQUEST", "message": "x"}
        )
        bank_account = FiatWithdrawalBankAccount(*["x"] * 8)
        with pytest.raises(APIError) as exc:
            client.request_fiat_withdrawal(Decimal("1"), bank_account)
        assert exc.value.error_code == "INVALID_WITHDRAWAL_REQUEST"

    @pytest.mark.parametrize("transfer_type", ["FIAT_DEPOSIT", "FIAT_WITHDRAWAL"])
    def test_pending_transfers_parse_fiat_types(self, transfer_type):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "items": [
                    {
                        "transferId": 3,
                        "transactionId": "3",
                        "amount": "10.00",
                        "symbol": "cash",
                        "type": transfer_type,
                        "status": "PENDING",
                    }
                ],
                "total": 1,
                "count": 1,
            },
        )
        [transfer] = client.get_pending_transfers(1, 10)
        assert transfer.type == TransactionType(transfer_type)
        assert transfer.transfer_id == 3

    @pytest.mark.parametrize(
        "item, transfer_id",
        [
            ({"transferId": 41}, 41),
            ({}, None),
        ],
    )
    def test_closed_transfers_parse_transfer_id(self, item, transfer_id):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "items": [
                    {
                        "transactionId": "0xabc",
                        "amount": "5.00",
                        "symbol": "USDC",
                        "type": "DEPOSIT",
                        "status": "DONE",
                        **item,
                    }
                ],
                "total": 1,
                "count": 1,
            },
        )
        [transfer] = client.get_closed_transfers(1, 10)
        assert transfer.type == TransactionType.DEPOSIT
        assert transfer.transfer_id == transfer_id

    def test_limit_order_cost_parses(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "total": "101.00",
                "serviceFee": "1.00",
                "serviceFeeRatePercentage": "1.00",
            },
        )
        cost = client.limit_order_cost(OrderSide.BUY, "AAPL", 1, Decimal("100"))
        assert cost.total == Decimal("101.00")
        assert cost.last_price is None
        _, url = session.request.call_args.args
        assert url.endswith("/orders/limit/buy/cost/")

    def test_market_sell_cost_parses_last_price(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "total": "9.90",
                "serviceFee": "0.10",
                "serviceFeeRatePercentage": "1.00",
                "lastPrice": "10.00",
            },
        )
        cost = client.market_sell_cost("AAPL", 1)
        assert cost.last_price == Decimal("10.00")

    def test_swappable_symbols_returns_list(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, ["AAPL", "TSLA"])
        assert client.swappable_symbols() == ["AAPL", "TSLA"]

    def test_application_settings_parses(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200, {"portfolioRefreshRate": 3000, "buyingDigitalIdentityFee": "0.00"}
        )
        settings = client.application_settings()
        assert settings.portfolio_refresh_rate == 3000
        assert settings.buying_digital_identity_fee == Decimal("0.00")

    def test_portfolio_history_parses(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                "range": "7d",
                "startValue": "100",
                "endValue": "110",
                "change": "10",
                "changePercentage": "10.00",
            },
        )
        history = client.portfolio_history("7d")
        assert history.range == "7d" and history.change == Decimal("10")

    def test_digital_identity_id_returns_token(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"tokenId": 47})
        assert client.digital_identity_id() == 47

    def test_digital_identity_id_none_when_absent(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(404, {"detail": ["no did"]})
        assert client.digital_identity_id() is None


class TestTransportLayer:
    def test_handle_204_returns_empty_dict_without_parsing(self):
        # Seed a body so the empty-content fallback can't mask a missing 204
        # guard: a 204 must return {} even when content is present.
        client, _ = _client_with_session()
        assert client._handle(_Resp(204, {"x": 1})) == {}

    def test_handle_200_empty_body_returns_empty_dict(self):
        client, _ = _client_with_session()
        assert client._handle(_Resp(200, content=b"")) == {}

    def test_handle_200_json_body_is_parsed(self):
        client, _ = _client_with_session()
        assert client._handle(_Resp(200, {"a": 1})) == {"a": 1}

    def test_handle_maps_status_codes(self):
        client, _ = _client_with_session()
        with pytest.raises(NotLoggedIn):
            client._handle(_Resp(401, {"detail": "x"}))
        with pytest.raises(AuthorizationError):
            client._handle(_Resp(403, {"detail": "x"}))
        with pytest.raises(APIError):
            client._handle(_Resp(400, {"errorCode": "NOPE"}))

    def test_origin_strips_path_from_base_url(self, monkeypatch):
        monkeypatch.setattr(
            "primedelta.primedelta_client.PRIMEDELTA_BASE_URL",
            "https://api.example.io/api/v2",
        )
        client, _ = _client_with_session()
        assert client._origin() == "https://api.example.io"

    def test_url_joins_endpoint_onto_base(self, monkeypatch):
        monkeypatch.setattr(
            "primedelta.primedelta_client.PRIMEDELTA_BASE_URL",
            "https://api.example.io",
        )
        client, _ = _client_with_session()
        assert client._url("/messages/") == "https://api.example.io/messages/"

    def test_error_code_non_json_body_is_none(self):
        assert PrimeDeltaClient._error_code(_Resp(400, content=b"<html>")) is None

    def test_error_code_list_body_is_none(self):
        assert PrimeDeltaClient._error_code(_Resp(400, ["a", "b"])) is None

    def test_error_code_prefers_errorcode_over_code(self):
        resp = _Resp(400, {"errorCode": "PRIMARY", "code": "secondary"})
        assert PrimeDeltaClient._error_code(resp) == "PRIMARY"

    def test_decimal_or_none(self):
        assert PrimeDeltaClient._decimal_or_none(None) is None
        assert PrimeDeltaClient._decimal_or_none("1.5") == Decimal("1.5")

    def test_get_passes_params_and_omits_csrf(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"ok": 1})
        assert client._get("/thing/", {"size": 100}) == {"ok": 1}
        kwargs = session.request.call_args.kwargs
        assert kwargs["params"] == {"size": 100}
        assert "X-CSRFToken" not in kwargs["headers"]
        session.get.assert_not_called()


class TestTimeoutSession:
    def _session(self):
        from primedelta.primedelta_client import _HTTP_TIMEOUT, _TimeoutSession

        return _TimeoutSession(), _HTTP_TIMEOUT

    def test_applies_default_timeout(self):
        from unittest.mock import patch

        s, default = self._session()
        with patch("requests.Session.request", return_value=MagicMock()) as sup:
            s.get("http://x")
        assert sup.call_args.kwargs["timeout"] == default

    def test_exempts_streaming_requests(self):
        from unittest.mock import patch

        s, _ = self._session()
        with patch("requests.Session.request", return_value=MagicMock()) as sup:
            s.get("http://x", stream=True)
        assert sup.call_args.kwargs.get("timeout") is None

    def test_explicit_timeout_wins(self):
        from unittest.mock import patch

        s, _ = self._session()
        with patch("requests.Session.request", return_value=MagicMock()) as sup:
            s.post("http://x", timeout=5)
        assert sup.call_args.kwargs["timeout"] == 5


class TestAISubaccounts:
    def test_register_ai_account_posts_agent_name_and_main(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(204)

        client.register_ai_account(
            agent_name="trader-bot", main_wallet_address="0xMAIN"
        )

        method, url = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/register-ai-account/")
        assert kwargs["json"] == {
            "agentName": "trader-bot",
            "mainWalletAddress": "0xMAIN",
        }
        session.get.assert_any_call(client._url("/csrf-token/"))
        assert kwargs["headers"]["X-CSRFToken"] == "tok"

    def test_get_pending_ai_agents_parses_the_list(self):
        from primedelta.types import PendingAIAgent

        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            [
                {"subWalletAddress": "0xSUB1", "agentName": "bot-1"},
                {"subWalletAddress": "0xSUB2", "agentName": "bot-2"},
            ],
        )

        assert client.get_pending_ai_agents() == [
            PendingAIAgent(sub_wallet_address="0xSUB1", agent_name="bot-1"),
            PendingAIAgent(sub_wallet_address="0xSUB2", agent_name="bot-2"),
        ]
        method, url = session.request.call_args.args
        assert method == "GET"
        assert url.endswith("/pending-ai-agents/")

    def test_confirm_ai_agent_posts_the_sub_wallet(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(204)

        client.confirm_ai_agent(sub_wallet_address="0xSUB")

        method, url = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/confirm-ai-agent/")
        assert kwargs["json"] == {"subWalletAddress": "0xSUB"}
        session.get.assert_any_call(client._url("/csrf-token/"))
        assert kwargs["headers"]["X-CSRFToken"] == "tok"

    def test_reject_ai_agent_posts_the_sub_wallet(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(204)

        client.reject_ai_agent(sub_wallet_address="0xSUB")

        method, url = session.request.call_args.args
        kwargs = session.request.call_args.kwargs
        assert method == "POST"
        assert url.endswith("/reject-ai-agent/")
        assert kwargs["json"] == {"subWalletAddress": "0xSUB"}
        session.get.assert_any_call(client._url("/csrf-token/"))
        assert kwargs["headers"]["X-CSRFToken"] == "tok"

    def test_get_my_ai_agents_parses_status_and_keeps_an_unknown_one(self):
        from primedelta.types import AccountStatus, AIAgent

        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            [
                {
                    "subWalletAddress": "0xSUB1",
                    "agentName": "bot-1",
                    "status": "VERIFIED_MINTED",
                },
                {"subWalletAddress": "0xSUB2", "agentName": "bot-2", "status": "X"},
            ],
        )

        assert client.get_my_ai_agents() == [
            AIAgent(
                sub_wallet_address="0xSUB1",
                agent_name="bot-1",
                status=AccountStatus.DID_MINTED,
            ),
            AIAgent(
                sub_wallet_address="0xSUB2",
                agent_name="bot-2",
                status=AccountStatus.UNKNOWN,
                raw_status="X",
            ),
        ]
        method, url = session.request.call_args.args
        assert method == "GET"
        assert url.endswith("/my-ai-agents/")

    @pytest.mark.parametrize(
        "agent_name, body",
        [
            (None, {"subWalletAddress": "0xSUB"}),
            ("bot", {"subWalletAddress": "0xSUB", "agentName": "bot"}),
        ],
    )
    def test_request_ai_agent_approval_posts_and_parses(self, agent_name, body):
        from datetime import datetime, timezone

        from primedelta.types import AIAgentApproval

        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(
            201,
            {
                "nonce": "ab" * 32,
                "expiresAt": "2026-10-04T12:10:00Z",
                "mainMessage": "main\ntext",
                "agentMessage": "agent\ntext",
            },
        )

        approval = client.request_ai_agent_approval("0xSUB", agent_name)

        assert approval == AIAgentApproval(
            nonce="ab" * 32,
            expires_at=datetime(2026, 10, 4, 12, 10, tzinfo=timezone.utc),
            main_message="main\ntext",
            agent_message="agent\ntext",
        )
        method, url = session.request.call_args.args
        assert method == "POST"
        assert url.endswith("/request-ai-agent-approval/")
        assert session.request.call_args.kwargs["json"] == body

    def test_confirm_ai_agent_posts_nonce_and_signature(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(204)

        client.confirm_ai_agent("0xSUB", nonce="n1", signature="0xsig")

        assert session.request.call_args.kwargs["json"] == {
            "subWalletAddress": "0xSUB",
            "nonce": "n1",
            "signature": "0xsig",
        }

    def test_link_ai_agent_posts_both_signatures(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(204)

        client.link_ai_agent(
            "0xSUB", nonce="n1", main_signature="0xmain", agent_signature="0xagent"
        )

        method, url = session.request.call_args.args
        assert method == "POST"
        assert url.endswith("/link-ai-agent/")
        assert session.request.call_args.kwargs["json"] == {
            "subWalletAddress": "0xSUB",
            "nonce": "n1",
            "mainSignature": "0xmain",
            "agentSignature": "0xagent",
        }

    _TRANSFER = {
        "transferId": 7,
        "kind": "FUND",
        "amount": "10.500000",
        "symbol": "USD",
        "fromWalletAddress": "0xMAIN",
        "toWalletAddress": "0xSUB",
        "createdAt": "2026-10-04T12:00:00.123456Z",
    }

    def test_fund_ai_agent_posts_a_string_amount_and_parses_the_transfer(self):
        from datetime import datetime, timezone

        from primedelta.types import InternalTransfer, InternalTransferKind

        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(201, self._TRANSFER)

        transfer = client.fund_ai_agent("0xSUB", Decimal("10.5"), "req-1")

        assert transfer == InternalTransfer(
            transfer_id=7,
            kind=InternalTransferKind.FUND,
            amount=Decimal("10.5"),
            symbol="USD",
            from_wallet_address="0xMAIN",
            to_wallet_address="0xSUB",
            created_at=datetime(2026, 10, 4, 12, 0, 0, 123456, tzinfo=timezone.utc),
        )
        method, url = session.request.call_args.args
        assert method == "POST"
        assert url.endswith("/fund-ai-agent/")
        assert session.request.call_args.kwargs["json"] == {
            "subWalletAddress": "0xSUB",
            "amount": "10.5",
            "requestId": "req-1",
        }

    @pytest.mark.parametrize("amount", [1.5, Decimal("0"), Decimal("-1"), True])
    def test_fund_ai_agent_rejects_a_bad_amount_before_posting(self, amount):
        client, session = _client_with_session()
        with pytest.raises((TypeError, ValueError)):
            client.fund_ai_agent("0xSUB", amount, "req-1")
        session.request.assert_not_called()

    @pytest.mark.parametrize(
        "sub_wallet_address, body",
        [
            (None, {"amount": "3", "requestId": "req-2"}),
            (
                "0xSUB",
                {"amount": "3", "requestId": "req-2", "subWalletAddress": "0xSUB"},
            ),
        ],
    )
    def test_return_to_main_names_the_agent_only_when_given(
        self, sub_wallet_address, body
    ):
        from primedelta.types import InternalTransferKind

        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(201, {**self._TRANSFER, "kind": "RETURN"})

        transfer = client.return_to_main(3, "req-2", sub_wallet_address)

        assert transfer.kind == InternalTransferKind.RETURN
        assert transfer.raw_kind is None
        method, url = session.request.call_args.args
        assert url.endswith("/return-to-main/")
        assert session.request.call_args.kwargs["json"] == body

    def test_internal_transfer_keeps_an_unknown_kind(self):
        from primedelta.types import InternalTransferKind

        transfer = PrimeDeltaClient._parse_internal_transfer(
            {**self._TRANSFER, "kind": "SWEEP"}
        )

        assert transfer.kind == InternalTransferKind.UNKNOWN
        assert transfer.raw_kind == "SWEEP"

    def test_transfer_error_code_surfaces_as_api_error(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(400, {"errorCode": "AGENT_NOT_FOUND"})

        with pytest.raises(APIError) as excinfo:
            client.fund_ai_agent("0xSUB", 1, "req-1")
        assert excinfo.value.error_code == "AGENT_NOT_FOUND"


class TestTransportRobustness:
    def test_connection_error_becomes_backend_unavailable(self):
        client, session = _client_with_session()
        session.request.side_effect = requests.exceptions.ConnectionError("aborted")
        with pytest.raises(BackendUnavailable):
            client._get("/portfolio/")

    def test_read_timeout_becomes_backend_unavailable(self):
        client, session = _client_with_session()
        session.request.side_effect = requests.exceptions.ReadTimeout("slow")
        with pytest.raises(BackendUnavailable):
            client._get("/portfolio/")

    def test_5xx_becomes_backend_unavailable(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(502)
        with pytest.raises(BackendUnavailable):
            client._get("/portfolio/")

    def test_session_get_5xx_becomes_backend_unavailable(self):
        # the direct-GET helpers (nonce/csrf/stocks/market-status) also type a
        # persistent 5xx, matching _handle and the PR's guarantee.
        client, session = _client_with_session()
        session.get.return_value = _Resp(503)
        with pytest.raises(BackendUnavailable):
            client.get_nonce()

    def test_session_get_transport_error_becomes_backend_unavailable(self):
        client, session = _client_with_session()
        session.get.side_effect = requests.exceptions.ConnectionError("nope")
        with pytest.raises(BackendUnavailable):
            client.get_nonce()

    def test_retry_adapter_retries_idempotent_gets_only(self):
        session = _TimeoutSession()
        retry = session.get_adapter("https://x/").max_retries
        assert retry.total == 2
        assert "GET" in retry.allowed_methods
        # never auto-retry a POST/PUT/PATCH/DELETE — could double-submit
        assert "POST" not in retry.allowed_methods
        assert 502 in retry.status_forcelist


class TestAutoRelogin:
    def test_retries_get_once_after_relogin_on_401(self):
        client, session = _client_with_session()
        session.request.side_effect = [_Resp(401), _Resp(200, {"ok": True})]
        calls = []
        client.set_relogin(lambda: calls.append(1))

        assert client._get("/me/") == {"ok": True}
        assert calls == [1]
        assert session.request.call_count == 2

    def test_no_relogin_when_not_armed(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(401)

        with pytest.raises(NotLoggedIn):
            client._get("/me/")
        assert session.request.call_count == 1

    def test_relogin_fires_at_most_once(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(401)  # stays unauthorized
        calls = []
        client.set_relogin(lambda: calls.append(1))

        with pytest.raises(NotLoggedIn):
            client._get("/me/")
        assert calls == [1]  # one relogin, then give up
        assert session.request.call_count == 2

    def test_reentrancy_guard_blocks_nested_relogin(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(401)
        calls = []

        def relogin():
            calls.append(1)
            # a call made *during* relogin must not trigger another relogin
            with pytest.raises(NotLoggedIn):
                client._get("/inner/")

        client.set_relogin(relogin)
        with pytest.raises(NotLoggedIn):
            client._get("/outer/")
        assert calls == [1]

    def test_relogin_retries_unsafe_post_after_401(self):
        client, session = _client_with_session()
        session.request.side_effect = [_Resp(401), _Resp(200, {"done": True})]
        # csrf token is fetched via session.get on the first unsafe call
        session.get.return_value = _Resp(200, {"csrfToken": "t"})
        calls = []
        client.set_relogin(lambda: calls.append(1))

        assert client._post("/x/", {"a": 1}) == {"done": True}
        assert calls == [1]

    def test_signed_price_updates_relogins_on_401(self):
        client, session = _client_with_session()
        session.request.side_effect = [
            _Resp(401),
            _Resp(200, [{"signature": "0xabcd"}]),
        ]
        calls = []
        client.set_relogin(lambda: calls.append(1))

        assert client.get_signed_price_updates(["AAPL"]) == [bytes.fromhex("abcd")]
        assert calls == [1]
        assert session.request.call_count == 2

    def test_digital_identity_id_relogins_on_401(self):
        client, session = _client_with_session()
        session.request.side_effect = [_Resp(401), _Resp(200, {"tokenId": 7})]
        calls = []
        client.set_relogin(lambda: calls.append(1))

        assert client.digital_identity_id() == 7
        assert calls == [1]


class TestSessionPersistence:
    def test_export_import_round_trips_cookies_with_domain(self):
        c1 = PrimeDeltaClient()
        c1._session.cookies.set(
            "sessionid", "abc", domain="api.dev.primedelta.io", path="/"
        )
        c1._session.cookies.set(
            "csrftoken", "xyz", domain="api.dev.primedelta.io", path="/"
        )
        exported = c1.export_session()
        assert {d["name"] for d in exported} == {"sessionid", "csrftoken"}

        c2 = PrimeDeltaClient()
        c2.import_session(exported)
        restored = {ck.name: ck.value for ck in c2._session.cookies}
        assert restored == {"sessionid": "abc", "csrftoken": "xyz"}
        # the domain survives so the restored cookie attaches to backend requests
        by_domain = {ck.name: ck.domain for ck in c2._session.cookies}
        assert by_domain["sessionid"] == "api.dev.primedelta.io"

    def test_import_clears_prior_cookies_and_resets_csrf(self):
        c = PrimeDeltaClient()
        c._session.cookies.set("stale", "1", domain="old", path="/")
        c._csrf_token = "cached"
        c.import_session(
            [{"name": "sessionid", "value": "new", "domain": "api", "path": "/"}]
        )
        assert {ck.name for ck in c._session.cookies} == {"sessionid"}
        assert c._csrf_token is None

    def test_export_is_empty_without_a_session(self):
        assert PrimeDeltaClient().export_session() == []


def _open_order_item(quantity, filled_quantity, price="150.00"):
    return {
        "id": 1,
        "submittedAt": "2026-09-30T14:00:00",
        "actionType": "BUY",
        "type": "LIMIT",
        "stockSymbol": "AAPL",
        "stockName": "Apple",
        "quantity": quantity,
        "filledQuantity": filled_quantity,
        "price": price,
        "value": "75.00",
        "dateOfCancellation": None,
    }


def _closed_order_item(quantity, filled_quantity, price="150.00"):
    return {
        "id": 2,
        "stockSymbol": "AAPL",
        "quantity": quantity,
        "filledQuantity": filled_quantity,
        "actionType": "SELL",
        "type": "LIMIT",
        "status": "CANCELED",
        "submittedAt": "2026-09-30T14:00:00",
        "closedAt": "2026-09-30T15:00:00",
        "averagePrice": "150.00",
        "price": price,
        "value": "75.00",
        "fees": None,
        "stockName": "Apple",
        "dateOfCancellation": "2026-10-01",
    }


_QUANTITY_FIXTURES = [
    ("0.5", "0.25"),
    ("2.75", "1.5"),
    ("10.000000000000000000", "0.000000000000000001"),
]


class TestOrderQuantities:
    @pytest.mark.parametrize("quantity, filled_quantity", _QUANTITY_FIXTURES)
    def test_open_orders_keep_fractional_quantities(self, quantity, filled_quantity):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200, {"items": [_open_order_item(quantity, filled_quantity)]}
        )
        [order] = client.open_orders(1, 10)
        assert order.quantity == Decimal(quantity)
        assert order.filled_quantity == Decimal(filled_quantity)
        assert isinstance(order.quantity, Decimal)
        assert str(order.quantity) == quantity

    @pytest.mark.parametrize("quantity, filled_quantity", _QUANTITY_FIXTURES)
    def test_closed_orders_keep_fractional_quantities(self, quantity, filled_quantity):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200, {"items": [_closed_order_item(quantity, filled_quantity)]}
        )
        [order] = client.closed_orders(1, 10)
        assert order.quantity == Decimal(quantity)
        assert order.filled_quantity == Decimal(filled_quantity)
        assert isinstance(order.quantity, Decimal)
        assert str(order.quantity) == quantity

    def test_order_prices_keep_18_decimal_places(self):
        client, session = _client_with_session()
        price = "150.123456789012345678"
        session.request.return_value = _Resp(
            200, {"items": [_open_order_item("1", "0", price)]}
        )
        assert client.open_orders(1, 10)[0].price == Decimal(price)
        session.request.return_value = _Resp(
            200, {"items": [_closed_order_item("1", "0", price)]}
        )
        assert client.closed_orders(1, 10)[0].price == Decimal(price)


_ANY_RESPONSE = {
    "orderId": 1,
    "withdrawalId": 2,
    "signature": "ab",
    "nonce": "0x1",
    "amount": "1",
    "total": None,
    "serviceFee": None,
    "serviceFeeRatePercentage": None,
}

_QUANTITY_CALLS = [
    pytest.param(
        lambda c, q: c.send_limit_order(q, "AAPL", OrderSide.BUY, Decimal("150"), None),
        id="send_limit_order",
    ),
    pytest.param(
        lambda c, q: c.send_sell_market_order(q, "AAPL"), id="send_sell_market_order"
    ),
    pytest.param(
        lambda c, q: c.limit_order_cost(OrderSide.SELL, "AAPL", q, Decimal("150")),
        id="limit_order_cost",
    ),
    pytest.param(lambda c, q: c.market_sell_cost("AAPL", q), id="market_sell_cost"),
    pytest.param(
        lambda c, q: c.get_deposit_stocks_signature(q, "AAPL"),
        id="get_deposit_stocks_signature",
    ),
    pytest.param(
        lambda c, q: c.request_stock_withdrawal(q, "AAPL"),
        id="request_stock_withdrawal",
    ),
    pytest.param(
        lambda c, q: c.get_deposit_stablecoin_signature(q, "dUSD"),
        id="get_deposit_stablecoin_signature",
    ),
]

_PRICE_CALLS = [
    pytest.param(
        lambda c, p: c.send_limit_order(1, "AAPL", OrderSide.BUY, p, None),
        id="send_limit_order",
    ),
    pytest.param(
        lambda c, p: c.limit_order_cost(OrderSide.BUY, "AAPL", 1, p),
        id="limit_order_cost",
    ),
]


def _sent(session, field):
    kwargs = session.request.call_args.kwargs
    return (kwargs["json"] or kwargs["params"])[field]


class TestQuantityArguments:
    @pytest.mark.parametrize("call", _QUANTITY_CALLS)
    @pytest.mark.parametrize(
        "quantity, wire",
        [(10, "10"), (Decimal("0.5"), "0.5"), (Decimal("1E+1"), "10")],
    )
    def test_int_and_decimal_are_sent_as_plain_strings(self, call, quantity, wire):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, _ANY_RESPONSE)
        call(client, quantity)
        assert _sent(session, "amount") == wire

    @pytest.mark.parametrize("call", _QUANTITY_CALLS)
    @pytest.mark.parametrize(
        "quantity, error",
        [
            (1.5, TypeError),
            (10.0, TypeError),
            (True, TypeError),
            ("10", TypeError),
            (Decimal("NaN"), ValueError),
            (Decimal("Infinity"), ValueError),
            (Decimal("0"), ValueError),
            (-1, ValueError),
        ],
    )
    def test_invalid_quantity_raises_before_any_http_call(self, call, quantity, error):
        client, session = _client_with_session()
        with pytest.raises(error):
            call(client, quantity)
        session.request.assert_not_called()
        session.get.assert_not_called()

    @pytest.mark.parametrize("call", _PRICE_CALLS)
    @pytest.mark.parametrize(
        "price, wire",
        [
            (Decimal("1.5E+2"), "150"),
            (Decimal("150.123456789012345678"), "150.123456789012345678"),
        ],
    )
    def test_price_limit_is_sent_without_exponent(self, call, price, wire):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, _ANY_RESPONSE)
        call(client, price)
        assert _sent(session, "priceLimit") == wire

    @pytest.mark.parametrize("call", _PRICE_CALLS)
    @pytest.mark.parametrize("price", [150.25, Decimal("0"), Decimal("NaN")])
    def test_invalid_price_limit_raises_before_any_http_call(self, call, price):
        client, session = _client_with_session()
        with pytest.raises((TypeError, ValueError)):
            call(client, price)
        session.request.assert_not_called()
        session.get.assert_not_called()


class TestWithdrawalAmountWire:
    def test_stablecoin_withdrawal_amount_is_sent_in_positional_notation(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 7})
        client.request_stablecoin_withdrawal(Decimal("1E+2"))
        assert session.request.call_args.kwargs["json"]["amount"] == "100"

    def test_fiat_withdrawal_amount_is_sent_in_positional_notation(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, {"withdrawalId": 12})
        client.request_fiat_withdrawal(
            Decimal("1E+2"), FiatWithdrawalBankAccount(*["x"] * 8)
        )
        assert session.request.call_args.kwargs["json"]["amount"] == "100"


class TestStablecoinDeposit:
    @pytest.mark.parametrize(
        "amount, wire",
        [
            (Decimal("10.50"), "10.50"),
            (Decimal("10.500"), "10.50"),
            (Decimal("0.01"), "0.01"),
            (Decimal("1E+1"), "10"),
            (10, "10"),
        ],
    )
    def test_cents_are_sent_as_given(self, amount, wire):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(
            200, {"signature": "ab", "nonce": "0x1", "amount": "10500000"}
        )
        signature = client.get_deposit_stablecoin_signature(amount, "dUSD")
        assert session.request.call_args.kwargs["json"] == {
            "amount": wire,
            "symbol": "dUSD",
        }
        assert signature.amount == "10500000"

    @pytest.mark.parametrize(
        "amount", [Decimal("10.505"), Decimal("0.001"), Decimal("10.5001")]
    )
    def test_more_than_two_decimal_places_raises_before_any_http_call(self, amount):
        client, session = _client_with_session()
        with pytest.raises(ValueError, match="at most 2 decimal places"):
            client.get_deposit_stablecoin_signature(amount, "dUSD")
        session.request.assert_not_called()
        session.get.assert_not_called()


def _page(*items):
    return _Resp(200, {"items": list(items), "total": len(items), "count": len(items)})


def _transfer_item(transfer_type, status):
    return {
        "transferId": 7,
        "transactionId": "0xabc",
        "amount": "5.00",
        "symbol": "USDC",
        "type": transfer_type,
        "status": status,
    }


def _distribution_item(distribution_type):
    return {
        "amount": "1.25",
        "type": distribution_type,
        "stockSymbol": "AAPL",
        "quantity": "3",
    }


def _known(enum_type):
    return [member for member in enum_type if member is not enum_type.UNKNOWN]


_TRANSFER_PAGES = [
    pytest.param(lambda c: c.get_pending_transfers(1, 10), id="pending"),
    pytest.param(lambda c: c.get_closed_transfers(1, 10), id="closed"),
]


class TestUnknownEnumValues:
    @pytest.mark.parametrize("fetch", _TRANSFER_PAGES)
    def test_unknown_transfer_type_keeps_the_row(self, fetch):
        client, session = _client_with_session()
        session.request.return_value = _page(
            _transfer_item("DEPOSIT", "DONE"),
            _transfer_item("CRYPTO_DEPOSIT", "DONE"),
        )
        known, unknown = fetch(client)
        assert (known.type, known.raw_type) == (TransactionType.DEPOSIT, None)
        assert (unknown.type, unknown.raw_type) == (
            TransactionType.UNKNOWN,
            "CRYPTO_DEPOSIT",
        )
        assert (unknown.status, unknown.raw_status) == (
            TransferHistoryStatus.DONE,
            None,
        )
        assert (unknown.transfer_id, unknown.amount) == (7, Decimal("5.00"))

    @pytest.mark.parametrize("fetch", _TRANSFER_PAGES)
    def test_unknown_transfer_status_keeps_the_row(self, fetch):
        client, session = _client_with_session()
        session.request.return_value = _page(
            _transfer_item("WITHDRAWAL", "PENDING"),
            _transfer_item("WITHDRAWAL", "ON_HOLD"),
        )
        known, unknown = fetch(client)
        assert (known.status, known.raw_status) == (
            TransferHistoryStatus.PENDING,
            None,
        )
        assert (unknown.status, unknown.raw_status) == (
            TransferHistoryStatus.UNKNOWN,
            "ON_HOLD",
        )
        assert (unknown.type, unknown.raw_type) == (TransactionType.WITHDRAWAL, None)

    @pytest.mark.parametrize("transfer_type", _known(TransactionType))
    @pytest.mark.parametrize("status", _known(TransferHistoryStatus))
    def test_known_transfer_values_carry_no_raw_value(self, transfer_type, status):
        client, session = _client_with_session()
        session.request.return_value = _page(
            _transfer_item(transfer_type.value, status.value)
        )
        [transfer] = client.get_closed_transfers(1, 10)
        assert (transfer.type, transfer.raw_type) == (transfer_type, None)
        assert (transfer.status, transfer.raw_status) == (status, None)

    def test_unknown_distribution_type_keeps_the_row(self):
        client, session = _client_with_session()
        session.request.return_value = _page(
            _distribution_item("DIVIDEND"), _distribution_item("SPIN_OFF")
        )
        known, unknown = client.get_distributions(1, 10)
        assert (known.type, known.raw_type) == (DistributionType.DIVIDEND, None)
        assert (unknown.type, unknown.raw_type) == (
            DistributionType.UNKNOWN,
            "SPIN_OFF",
        )
        assert (unknown.amount, unknown.stock_quantity) == (
            Decimal("1.25"),
            Decimal("3"),
        )

    @pytest.mark.parametrize("distribution_type", _known(DistributionType))
    def test_known_distribution_types_carry_no_raw_value(self, distribution_type):
        client, session = _client_with_session()
        session.request.return_value = _page(
            _distribution_item(distribution_type.value)
        )
        [distribution] = client.get_distributions(1, 10)
        assert (distribution.type, distribution.raw_type) == (distribution_type, None)

    def test_unknown_closed_order_status_keeps_the_row(self):
        client, session = _client_with_session()
        session.request.return_value = _page(
            _closed_order_item("2", "1"),
            {**_closed_order_item("2", "1"), "status": "EXPIRED"},
        )
        known, unknown = client.closed_orders(1, 10)
        assert (known.status, known.raw_status) == (OrderStatus.CANCELED, None)
        assert (unknown.status, unknown.raw_status) == (OrderStatus.UNKNOWN, "EXPIRED")
        assert (unknown.order_side, unknown.filled_quantity) == (
            OrderSide.SELL,
            Decimal("1"),
        )

    @pytest.mark.parametrize("status", [OrderStatus.EXECUTED, OrderStatus.CANCELED])
    def test_known_closed_order_statuses_carry_no_raw_value(self, status):
        client, session = _client_with_session()
        session.request.return_value = _page(
            {**_closed_order_item("1", "1"), "status": status.value}
        )
        [order] = client.closed_orders(1, 10)
        assert (order.status, order.raw_status) == (status, None)

    def test_unknown_account_status_parses_as_unknown(self):
        from primedelta.types import AccountStatus

        client, session = _client_with_session()
        session.request.return_value = _Resp(200, {"status": "SUSPENDED"})
        assert client.get_account_status() == AccountStatus.UNKNOWN


class TestInternalTransfers:
    @pytest.mark.parametrize("fetch", _TRANSFER_PAGES)
    @pytest.mark.parametrize("transfer_type", ["INTERNAL_OUT", "INTERNAL_IN"])
    def test_internal_rows_parse_like_fiat_cash_rows(self, fetch, transfer_type):
        client, session = _client_with_session()
        fiat_row = {
            **_transfer_item("FIAT_DEPOSIT", "DONE"),
            "symbol": "cash",
            "amount": "25.50",
        }
        session.request.return_value = _page(
            fiat_row, {**fiat_row, "type": transfer_type}
        )
        fiat, internal = fetch(client)
        assert internal == dataclasses.replace(
            fiat, type=TransactionType(transfer_type)
        )
        assert (internal.symbol, internal.amount) == ("cash", Decimal("25.50"))


_PORTFOLIO_BODY = {
    "balance": {
        "available": "10",
        "equity": "20",
        "funds": "10",
        "profitLoss": "0",
        "totalValue": "30",
    },
    "stocks": [
        {
            "symbol": "AAPL",
            "name": "Apple",
            "totalOwned": "2",
            "availableToSell": "2",
            "averagePurchasePrice": "10",
            "lastMarketPrice": "10",
            "profitLoss": "0",
            "profitLossPercentage": "0",
            "isOffboarded": False,
            "multiplierNumerator": 1,
            "multiplierDenominator": 1,
        }
    ],
}
_POLICY_BODY = {
    "subWalletAddress": "0xSUB",
    "paused": True,
    "allowedSymbols": ["AAPL", "MSFT"],
    "maxOrderUsd": "500.000000",
    "maxDailyUsd": "1000.500000",
    "day": "2026-10-04",
    "usedTodayUsd": "120.250000",
    "remainingTodayUsd": "880.250000",
    "resetsAt": "2026-10-05T00:00:00Z",
}


class TestAIAgentOversight:
    def test_my_ai_agents_parses_the_policy_fields(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            [
                {
                    "subWalletAddress": "0xSUB",
                    "agentName": "bot",
                    "status": "CLOSED",
                    "paused": True,
                    "allowedSymbols": ["AAPL"],
                    "maxOrderUsd": "500.000000",
                    "maxDailyUsd": None,
                }
            ],
        )
        from primedelta.types import AccountStatus

        [agent] = client.get_my_ai_agents()

        assert agent.status == AccountStatus.CLOSED
        assert agent.raw_status is None
        assert agent.paused is True
        assert agent.allowed_symbols == ["AAPL"]
        assert agent.max_order_usd == Decimal("500")
        assert agent.max_daily_usd is None

    def test_agent_portfolio_reads_like_the_own_portfolio(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(200, _PORTFOLIO_BODY)

        agent = client.get_ai_agent_portfolio("0xSUB")
        own = client.portfolio()

        assert agent == own
        first = session.request.call_args_list[0]
        assert first.args[0] == "GET"
        assert first.args[1].endswith("/ai-agent-portfolio/")
        assert first.kwargs["params"] == {"subWalletAddress": "0xSUB"}

    @pytest.mark.parametrize(
        "fetch, own, path, item",
        [
            (
                lambda c: c.get_ai_agent_open_orders("0xSUB", 2, 50),
                lambda c: c.open_orders(2, 50),
                "/ai-agent-open-orders/",
                _open_order_item("3", "1"),
            ),
            (
                lambda c: c.get_ai_agent_closed_orders("0xSUB", 2, 50),
                lambda c: c.closed_orders(2, 50),
                "/ai-agent-closed-orders/",
                _closed_order_item("3", "1"),
            ),
        ],
        ids=["open", "closed"],
    )
    def test_agent_orders_read_like_the_own_orders(self, fetch, own, path, item):
        client, session = _client_with_session()
        session.request.return_value = _page(item)

        assert fetch(client) == own(client)
        first = session.request.call_args_list[0]
        assert first.args[1].endswith(path)
        assert first.kwargs["params"] == {
            "subWalletAddress": "0xSUB",
            "page": 2,
            "size": 50,
        }

    @pytest.mark.parametrize(
        "call, path",
        [
            (lambda c: c.close_ai_agent("0xSUB"), "/close-ai-agent/"),
            (lambda c: c.reopen_ai_agent("0xSUB"), "/reopen-ai-agent/"),
        ],
        ids=["close", "reopen"],
    )
    def test_close_and_reopen_post_the_sub_wallet(self, call, path):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(204)

        call(client)

        method, url = session.request.call_args.args
        assert (method, url.endswith(path)) == ("POST", True)
        assert session.request.call_args.kwargs["json"] == {"subWalletAddress": "0xSUB"}

    @pytest.mark.parametrize(
        "sub, params", [(None, None), ("0xSUB", {"subWalletAddress": "0xSUB"})]
    )
    def test_get_policy_names_the_agent_only_when_given(self, sub, params):
        from datetime import date, datetime, timezone

        from primedelta.types import AIAgentPolicy

        client, session = _client_with_session()
        session.request.return_value = _Resp(200, _POLICY_BODY)

        policy = client.get_ai_agent_policy(sub)

        assert policy == AIAgentPolicy(
            sub_wallet_address="0xSUB",
            paused=True,
            allowed_symbols=["AAPL", "MSFT"],
            max_order_usd=Decimal("500"),
            max_daily_usd=Decimal("1000.5"),
            day=date(2026, 10, 4),
            used_today_usd=Decimal("120.25"),
            remaining_today_usd=Decimal("880.25"),
            resets_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
        )
        method, url = session.request.call_args.args
        assert (method, url.endswith("/ai-agent-policy/")) == ("GET", True)
        assert session.request.call_args.kwargs["params"] == params

    def test_an_unrestricted_policy_keeps_its_nulls(self):
        client, session = _client_with_session()
        session.request.return_value = _Resp(
            200,
            {
                **_POLICY_BODY,
                "paused": False,
                "allowedSymbols": None,
                "maxOrderUsd": None,
                "maxDailyUsd": None,
                "remainingTodayUsd": None,
            },
        )

        policy = client.get_ai_agent_policy()

        assert policy.allowed_symbols is None
        assert policy.max_order_usd is None
        assert policy.max_daily_usd is None
        assert policy.remaining_today_usd is None

    def test_set_policy_puts_every_key_with_cent_caps(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(200, _POLICY_BODY)

        client.set_ai_agent_policy("0xSUB", False, [], Decimal("500"), None)

        method, url = session.request.call_args.args
        assert (method, url.endswith("/ai-agent-policy/")) == ("PUT", True)
        assert session.request.call_args.kwargs["json"] == {
            "subWalletAddress": "0xSUB",
            "paused": False,
            "allowedSymbols": [],
            "maxOrderUsd": "500",
            "maxDailyUsd": None,
        }

    @pytest.mark.parametrize(
        "cap", [Decimal("0"), Decimal("-1"), Decimal("10.005"), 1.5, True]
    )
    def test_set_policy_rejects_a_bad_cap_before_sending(self, cap):
        client, session = _client_with_session()
        with pytest.raises((TypeError, ValueError)):
            client.set_ai_agent_policy("0xSUB", False, None, None, cap)
        session.request.assert_not_called()

    def test_a_paused_agent_403_is_an_api_error_without_a_csrf_retry(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        session.request.return_value = _Resp(403, {"errorCode": "AGENT_PAUSED"})

        with pytest.raises(APIError) as excinfo:
            client.send_sell_market_order(1, "AAPL")

        assert excinfo.value.error_code == "AGENT_PAUSED"
        assert session.request.call_count == 1

    def test_a_refusal_keeps_its_extra_fields_on_the_payload(self):
        client, session = _client_with_session()
        session.get.return_value = _Resp(200, {"csrfToken": "tok"})
        body = {
            "errorCode": "AGENT_DAILY_LIMIT_EXCEEDED",
            "limitUsd": "100.000000",
            "remainingUsd": "20.000000",
            "orderValueUsd": "30.000000",
        }
        session.request.return_value = _Resp(400, body)

        with pytest.raises(APIError) as excinfo:
            client.send_sell_market_order(1, "AAPL")

        assert excinfo.value.payload == body
