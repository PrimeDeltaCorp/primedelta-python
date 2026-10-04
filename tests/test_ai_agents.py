import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

from primedelta import (
    AIAgentApprovalError,
    AIAgentError,
    AIAgentTransferError,
    CannotCraft,
    LocalAccountSigner,
    MockBrowserSigner,
    NotEnoughFunds,
    PrimeDelta,
)
from primedelta.primedelta_client import APIError

MAIN_KEY = "0x" + "1" * 64
AGENT_KEY = "0x" + "2" * 64
MAIN = Account.from_key(MAIN_KEY).address
AGENT = Account.from_key(AGENT_KEY).address
NONCE = "c0" * 32
AGENT_NAME = 'Trader ✓ "one"'
_DETAILS = (
    f"Main account: {MAIN}\nChain ID: 2028\nNonce: {NONCE}\n"
    "Expires at: 2026-10-04T12:10:00+00:00"
)
MAIN_MESSAGE = (
    "PrimeDelta: approve an AI agent\n\n"
    f"I approve the wallet {AGENT} as my AI agent "
    f"{json.dumps(AGENT_NAME, ensure_ascii=False)} and ask PrimeDelta to issue it "
    f"a digital identity.\n\n{_DETAILS}"
)
AGENT_MESSAGE = (
    "PrimeDelta: link an AI agent\n\n"
    f"I am the wallet {AGENT} and I agree to become the AI agent "
    f"{json.dumps(AGENT_NAME, ensure_ascii=False)} of {MAIN}.\n\n{_DETAILS}"
)
APPROVAL = {
    "nonce": NONCE,
    "expiresAt": "2026-10-04T12:10:00Z",
    "mainMessage": MAIN_MESSAGE,
    "agentMessage": AGENT_MESSAGE,
}
TRANSFER = {
    "transferId": 7,
    "kind": "FUND",
    "amount": "10.000000",
    "symbol": "USD",
    "fromWalletAddress": MAIN,
    "toWalletAddress": AGENT,
    "createdAt": "2026-10-04T12:00:00.123456Z",
}


class _Resp:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data
        self.content = b"" if json_data is None else b"{}"

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _pd(signer=None):
    with patch("primedelta.primedelta.Web3"):
        if signer is None:
            return PrimeDelta(private_key=MAIN_KEY, web3_provider_url="http://x")
        return PrimeDelta(signer=signer, web3_provider_url="http://x")


def _pd_over_http(responses, signer=None):
    pd = _pd(signer)
    session = MagicMock()
    session.get.return_value = _Resp(200, {"csrfToken": "tok"})
    session.request.side_effect = responses
    pd._primedelta_client._session = session
    return pd, session


def _posts(session):
    return [
        (call.args[1].rsplit("/", 2)[-2], call.kwargs["json"])
        for call in session.request.call_args_list
    ]


def _signer_of(message, signature):
    return Account.recover_message(encode_defunct(text=message), signature=signature)


def _pd_with_mock_client():
    pd = _pd()
    pd._primedelta_client = MagicMock()
    return pd


class TestSignedConfirm:
    @pytest.mark.parametrize(
        "signer",
        [
            LocalAccountSigner.from_key(MAIN_KEY),
            MockBrowserSigner.from_key(MAIN_KEY),
        ],
        ids=["local", "wallet"],
    )
    def test_signs_the_servers_main_message_and_confirms_with_its_nonce(self, signer):
        pd, session = _pd_over_http([_Resp(201, APPROVAL), _Resp(204)], signer)

        pd.confirm_ai_agent(AGENT)

        (request_path, request_body), (confirm_path, confirm_body) = _posts(session)
        assert request_path == "request-ai-agent-approval"
        assert request_body == {"subWalletAddress": AGENT}
        assert confirm_path == "confirm-ai-agent"
        assert confirm_body["subWalletAddress"] == AGENT
        assert confirm_body["nonce"] == NONCE
        assert _signer_of(MAIN_MESSAGE, confirm_body["signature"]) == MAIN
        assert _signer_of(MAIN_MESSAGE + " ", confirm_body["signature"]) != MAIN
        assert _signer_of(AGENT_MESSAGE, confirm_body["signature"]) != MAIN

    def test_does_not_confirm_when_the_approval_request_fails(self):
        pd = _pd_with_mock_client()
        pd._signer = MagicMock()
        pd._primedelta_client.request_ai_agent_approval.side_effect = APIError(
            "AGENT_NAME_REQUIRED"
        )

        with pytest.raises(AIAgentError) as excinfo:
            pd.confirm_ai_agent(AGENT)

        assert excinfo.value.error_code == "AGENT_NAME_REQUIRED"
        assert not isinstance(excinfo.value, AIAgentApprovalError)
        pd._signer.sign_message.assert_not_called()
        pd._primedelta_client.confirm_ai_agent.assert_not_called()

    def test_maps_an_expired_approval_with_a_default_reason(self):
        pd, _ = _pd_over_http(
            [_Resp(201, APPROVAL), _Resp(400, {"errorCode": "APPROVAL_EXPIRED"})]
        )

        with pytest.raises(AIAgentApprovalError) as excinfo:
            pd.confirm_ai_agent(AGENT)

        assert isinstance(excinfo.value, APIError)
        assert excinfo.value.error_code == "APPROVAL_EXPIRED"
        assert excinfo.value.message == "the approval expired; request a new one"

    def test_a_backend_message_wins_over_the_default_reason(self):
        pd = _pd_with_mock_client()
        pd._primedelta_client.request_ai_agent_approval.side_effect = APIError(
            "INVALID_APPROVAL", "from the server"
        )

        with pytest.raises(AIAgentApprovalError) as excinfo:
            pd.confirm_ai_agent(AGENT)

        assert excinfo.value.message == "from the server"


class TestLink:
    def test_sends_the_main_and_agent_signatures_over_their_own_messages(self):
        pd, session = _pd_over_http([_Resp(201, APPROVAL), _Resp(204)])
        agent_signer = LocalAccountSigner.from_key(AGENT_KEY)

        approval = pd.request_ai_agent_approval(AGENT, agent_name=AGENT_NAME)
        pd.link_ai_agent(
            AGENT, approval, agent_signer.sign_message(approval.agent_message)
        )

        (request_path, request_body), (link_path, link_body) = _posts(session)
        assert request_path == "request-ai-agent-approval"
        assert request_body == {"subWalletAddress": AGENT, "agentName": AGENT_NAME}
        assert link_path == "link-ai-agent"
        assert link_body["subWalletAddress"] == AGENT
        assert link_body["nonce"] == NONCE
        assert _signer_of(MAIN_MESSAGE, link_body["mainSignature"]) == MAIN
        assert _signer_of(AGENT_MESSAGE, link_body["agentSignature"]) == AGENT

    def test_maps_a_link_error(self):
        pd = _pd_with_mock_client()
        pd._signer = MagicMock()
        pd._primedelta_client.link_ai_agent.side_effect = APIError(
            "SUBACCOUNT_LIMIT_REACHED"
        )
        approval = MagicMock(nonce=NONCE, main_message=MAIN_MESSAGE)

        with pytest.raises(AIAgentError) as excinfo:
            pd.link_ai_agent(AGENT, approval, "0xagent")

        assert excinfo.value.error_code == "SUBACCOUNT_LIMIT_REACHED"
        pd._signer.sign_message.assert_called_once_with(MAIN_MESSAGE)


class TestErrorMapping:
    @pytest.mark.parametrize(
        "code, error_type",
        [
            ("SELF_LINK_NOT_ALLOWED", AIAgentError),
            ("AGENT_NAME_REQUIRED", AIAgentError),
            ("SUBACCOUNT_NOT_FOUND", AIAgentError),
            ("MAIN_ACCOUNT_NOT_VERIFIED", AIAgentError),
            ("SUBACCOUNT_NOT_AWAITING_CONFIRMATION", AIAgentError),
            ("SUBACCOUNT_NOT_EMPTY", AIAgentError),
            ("SUBACCOUNT_NOT_ELIGIBLE", AIAgentError),
            ("ALREADY_SUBACCOUNT", AIAgentError),
            ("MAIN_ACCOUNT_NOT_FOUND", AIAgentError),
            ("MAIN_IS_SUBACCOUNT", AIAgentError),
            ("SUBACCOUNT_LIMIT_REACHED", AIAgentError),
            ("APPROVAL_SIGNATURE_REQUIRED", AIAgentApprovalError),
            ("INVALID_APPROVAL", AIAgentApprovalError),
            ("APPROVAL_EXPIRED", AIAgentApprovalError),
            ("INVALID_APPROVAL_SIGNATURE", AIAgentApprovalError),
            ("NOT_A_MAIN_ACCOUNT", AIAgentTransferError),
            ("AGENT_NOT_FOUND", AIAgentTransferError),
            ("AGENT_ADDRESS_REQUIRED", AIAgentTransferError),
            ("ACCOUNT_NOT_ACTIVE", AIAgentTransferError),
            ("INVALID_AMOUNT", AIAgentTransferError),
            ("REQUEST_ID_CONFLICT", AIAgentTransferError),
        ],
    )
    def test_each_code_maps_to_its_type_with_a_reason(self, code, error_type):
        pd = _pd_with_mock_client()
        pd._primedelta_client.fund_ai_agent.side_effect = APIError(code, None, "d")

        with pytest.raises(error_type) as excinfo:
            pd.fund_ai_agent(AGENT, Decimal("1"))

        assert type(excinfo.value) is error_type
        assert excinfo.value.error_code == code
        assert excinfo.value.message
        assert excinfo.value.detail == "d"

    @pytest.mark.parametrize("method", ["fund_ai_agent", "return_to_main"])
    def test_insufficient_funds_is_not_enough_funds(self, method):
        pd = _pd_with_mock_client()
        getattr(pd._primedelta_client, method).side_effect = APIError(
            "INSUFFICIENT_FUNDS"
        )

        with pytest.raises(NotEnoughFunds):
            if method == "fund_ai_agent":
                pd.fund_ai_agent(AGENT, Decimal("1"))
            else:
                pd.return_to_main(Decimal("1"))

    def test_an_unrelated_code_stays_a_plain_api_error(self):
        pd = _pd_with_mock_client()
        pd._primedelta_client.return_to_main.side_effect = APIError("INVALID_REQUEST")

        with pytest.raises(APIError) as excinfo:
            pd.return_to_main(Decimal("1"))

        assert type(excinfo.value) is APIError

    @pytest.mark.parametrize(
        "method, args",
        [
            ("register_ai_account", ("bot", MAIN)),
            ("reject_ai_agent", (AGENT,)),
            ("request_ai_agent_approval", (AGENT,)),
        ],
    )
    def test_the_other_agent_calls_map_too(self, method, args):
        pd = _pd_with_mock_client()
        getattr(pd._primedelta_client, method).side_effect = APIError(
            "SUBACCOUNT_NOT_AWAITING_CONFIRMATION"
        )

        with pytest.raises(AIAgentError):
            getattr(pd, method)(*args)


class TestTransfers:
    def test_fund_sends_a_fresh_uuid4_request_id_by_default(self):
        pd, session = _pd_over_http([_Resp(201, TRANSFER), _Resp(201, TRANSFER)])

        pd.fund_ai_agent(AGENT, Decimal("10"))
        pd.fund_ai_agent(AGENT, Decimal("10"))

        (_, first), (_, second) = _posts(session)
        assert uuid.UUID(first["requestId"]).version == 4
        assert first["requestId"] != second["requestId"]
        assert first["subWalletAddress"] == AGENT
        assert first["amount"] == "10"

    def test_fund_passes_the_callers_request_id(self):
        pd = _pd_with_mock_client()

        pd.fund_ai_agent(AGENT, Decimal("10"), request_id="retry-me")

        pd._primedelta_client.fund_ai_agent.assert_called_once_with(
            AGENT, Decimal("10"), "retry-me"
        )

    def test_return_from_the_agent_omits_the_sub_wallet(self):
        pd, session = _pd_over_http([_Resp(201, {**TRANSFER, "kind": "RETURN"})])

        transfer = pd.return_to_main(Decimal("2.5"))

        ((path, body),) = _posts(session)
        assert path == "return-to-main"
        assert set(body) == {"amount", "requestId"}
        assert uuid.UUID(body["requestId"]).version == 4
        assert transfer.amount == Decimal("10")

    def test_return_from_the_main_names_the_agent(self):
        pd = _pd_with_mock_client()

        pd.return_to_main(Decimal("2.5"), sub_wallet_address=AGENT, request_id="r")

        pd._primedelta_client.return_to_main.assert_called_once_with(
            Decimal("2.5"), "r", AGENT
        )

    @pytest.mark.parametrize("method", ["fund_ai_agent", "return_to_main"])
    def test_cannot_be_crafted(self, method):
        pd = _pd_with_mock_client()

        def action():
            if method == "fund_ai_agent":
                pd.fund_ai_agent(AGENT, Decimal("1"))
            else:
                pd.return_to_main(Decimal("1"))

        with pytest.raises(CannotCraft):
            pd.craft(action)
        getattr(pd._primedelta_client, method).assert_not_called()

    def test_my_ai_agents_delegates(self):
        pd = _pd_with_mock_client()
        pd._primedelta_client.get_my_ai_agents.return_value = ["agent"]

        assert pd.get_my_ai_agents() == ["agent"]
