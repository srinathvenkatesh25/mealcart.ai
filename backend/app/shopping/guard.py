"""Hard block on anything that could place an order or touch payment.

Installed on every browser context the app opens. It aborts page loads and
API calls for checkout, order placement and payment, whatever clicked them.
Cart edits are untouched. You check out yourself in your own browser or app;
the cart is saved to your account.
"""

import json
import re
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import BrowserContext, Route

BLOCKED_URL = re.compile(r"/checkout|/orders?/|/order_|place_?order|/payments?\b|/wallet", re.I)
# Instacart's web app talks to a GraphQL endpoint; block order/payment operations by name.
BLOCKED_OPERATION = re.compile(r"checkout|place_?order|submit_?order|payment|wallet", re.I)


def _operation_names(post_data: str | None) -> list[str]:
    if not post_data:
        return []
    try:
        body = json.loads(post_data)
    except ValueError:
        return []
    ops = body if isinstance(body, list) else [body]
    return [str(op.get("operationName", "")) for op in ops if isinstance(op, dict)]


def is_blocked(url: str, post_data: str | None = None) -> str | None:
    """Reason the request must not happen, or None if it's allowed."""
    parts = urlsplit(url)
    if BLOCKED_URL.search(parts.path):
        return f"url {url}"
    # GraphQL operations arrive in a POST body, or in the query string for GETs.
    names = _operation_names(post_data) + parse_qs(parts.query).get("operationName", [])
    for name in names:
        if BLOCKED_OPERATION.search(name):
            return f"operation {name}"
    return None


def _body_text(body: bytes | None) -> str | None:
    """Request body as text. Some bodies are binary (compressed analytics beacons),
    so decode leniently; binary bodies just won't parse as GraphQL."""
    return body.decode("utf-8", errors="replace") if body else None


async def install(ctx: BrowserContext, blocked_log: list[str]) -> None:
    async def handler(route: Route) -> None:
        req = route.request
        try:
            reason = is_blocked(req.url, _body_text(req.post_data_buffer) if req.method == "POST" else None)
        except Exception as e:  # fail closed: an unreadable request is never let through
            reason = f"guard could not check {req.url}: {type(e).__name__}"
        if reason:
            blocked_log.append(reason)
            await route.abort("blockedbyclient")
        else:
            await route.fallback()

    await ctx.route("**/*", handler)
