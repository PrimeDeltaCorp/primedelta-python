import os
from dataclasses import dataclass

SIWE_MESSAGE: str = (
    "By signing this message you confirm that you have completely"
    " read and understand Prime Delta's terms of service including all policies"
    " and disclosures and that you agree with each part of them."
)
USDC_ASSET_TYPE: str = "USDC"

PRIMEDELTA_BASE_URL: str = os.getenv(
    "PRIMEDELTA_BASE_URL", "https://api.dev.primedelta.io"
)
PRIMEDELTA_APP_URL: str = os.getenv(
    "PRIMEDELTA_APP_URL", "https://mint.dev.primedelta.io"
)
SIWE_URI: str = PRIMEDELTA_APP_URL
# Backend may strip the port (e.g. accepts "localhost" not "localhost:5173").
# Override via PRIMEDELTA_SIWE_DOMAIN when the backend's allowed domain differs.
SIWE_DOMAIN: str = os.getenv(
    "PRIMEDELTA_SIWE_DOMAIN",
    PRIMEDELTA_APP_URL.replace("https://", "").replace("http://", ""),
)

# Per-network default endpoints so `PrimeDelta(network=...)` targets the right
# backend without manual env. Env vars, when set, override for ALL networks
# (they are process-global) — that keeps the local-stack / integration-harness
# contract that already drives everything through PRIMEDELTA_BASE_URL/APP_URL.
_NETWORK_ENDPOINTS: dict[str, tuple[str, str]] = {
    "dev": ("https://api.dev.primedelta.io", "https://mint.dev.primedelta.io"),
    "testnet": (
        "https://api.testnet.primedelta.io",
        "https://mint.testnet.primedelta.io",
    ),
    "mainnet": (
        "https://api.primedelta.io",
        "https://mint.primedelta.io",
    ),
}


def _strip_scheme(url: str) -> str:
    return url.replace("https://", "").replace("http://", "")


@dataclass(frozen=True)
class Endpoints:
    base_url: str
    app_url: str
    siwe_domain: str
    siwe_uri: str


def resolve_endpoints(network: str) -> Endpoints:
    """Resolve backend + SIWE endpoints for a network.

    Precedence: explicit env override (PRIMEDELTA_BASE_URL / PRIMEDELTA_APP_URL /
    PRIMEDELTA_SIWE_DOMAIN) first, then the per-network default. Raises if a
    network has no default and no env override supplies the missing URL.
    """
    base_default, app_default = _NETWORK_ENDPOINTS.get(network, (None, None))
    base_url = os.getenv("PRIMEDELTA_BASE_URL") or base_default
    app_url = os.getenv("PRIMEDELTA_APP_URL") or app_default
    if base_url is None or app_url is None:
        raise ValueError(
            f"no default endpoints for network {network!r}; set "
            "PRIMEDELTA_BASE_URL and PRIMEDELTA_APP_URL"
        )
    siwe_domain = os.getenv("PRIMEDELTA_SIWE_DOMAIN") or _strip_scheme(app_url)
    return Endpoints(
        base_url=base_url, app_url=app_url, siwe_domain=siwe_domain, siwe_uri=app_url
    )


# Pyth Hermes API for public price feeds
# Default empty: the free hermes.pyth.network endpoint shuts down 2026-07-31.
# Set PYTH_HERMES_BASE_URL to an authenticated Hermes endpoint to re-enable
# the public Pyth stream (implementation is kept, integration is parked).
PYTH_HERMES_BASE_URL: str = os.getenv("PYTH_HERMES_BASE_URL", "")

BLOCKCHAIN_FALSE_VALUE = 2
