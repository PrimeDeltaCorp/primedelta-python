import os
from decimal import Decimal

from dotenv import find_dotenv, load_dotenv

from primedelta import LocalAccountSigner, PrimeDelta

load_dotenv(find_dotenv(".env.local") or find_dotenv(".env"))

main = PrimeDelta(
    private_key=os.environ["PRIMEDELTA_TEST_PRIVATE_KEY"],
    web3_provider_url=os.environ["PRIMEDELTA_PROVIDER_URL"],
)
main.login()

for pending in main.get_pending_ai_agents():
    main.confirm_ai_agent(pending.sub_wallet_address)

agent_signer = LocalAccountSigner.from_key(os.environ["PRIMEDELTA_AGENT_PRIVATE_KEY"])
approval = main.request_ai_agent_approval(agent_signer.address, agent_name="bot-1")
main.link_ai_agent(
    agent_signer.address,
    approval,
    agent_signer.sign_message(approval.agent_message),
)

agents = main.get_my_ai_agents()
funded = main.fund_ai_agent(agent_signer.address, Decimal("100"), request_id="fund-1")
returned = main.return_to_main(
    Decimal("40"), sub_wallet_address=agent_signer.address, request_id="return-1"
)

main.logout()
