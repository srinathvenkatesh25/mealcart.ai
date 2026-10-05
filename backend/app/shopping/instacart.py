"""Instacart page adapter: every selector and every bit of page text parsing.

Deterministic Playwright only. Selectors use roles and accessible names seen
on the live site (Oct 2026):
- search results live in a region named 'Results for "<query>"';
- each product is a group named "Product" with an h3 name, a
  "Current price: $X" line, a size line and an "Add 1 <unit> <name>" button;
- right after adding, the card shows "Quantity: N <unit>" and buttons
  "Increment quantity of <name>" / "Remove <name>" / "Decrement quantity of <name>";
- on a later page load, an in-cart product shows only a collapsed
  "Quantity: N ct. Change quantity" button; clicking it reveals the +/- buttons;
- the cart opens from a "View Cart. Items in cart: N…" button as a dialog,
  one cart per store.
Ads ("Sponsored") appear inside results and are skipped.
"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote_plus

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.shopping.packs import G_PER, parse_size
from app.config import get_settings
from app.shopping.session import (
    Ask, BrowserSession, captcha_present, ensure_logged_in, human_pause, login,
)

BASE = "https://www.instacart.com"
RESULTS_TIMEOUT_MS = 15000
MAX_CLICKS_PER_ITEM = 12

PRICE_RE = re.compile(r"Current price:\s*\$([\d,]+(?:\.\d+)?)")
SIZE_LINE_RE = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:fl\.?\s*oz|oz|lbs?|ct|count|each|g|kg|gal|qt|pt|l|ml|pk|pack)\b", re.I
)
ABOUT_RE = re.compile(r"About\s+(\d+(?:\.\d+)?)\s*lb", re.I)
QTY_RE = re.compile(r"Quantity:\s*([\d.]+)\s*([a-z]+)?", re.I)
IN_CART_BUTTON_RE = re.compile(r"^(Quantity:|Increment quantity of)", re.I)
ADD_UNIT_RE = re.compile(r"^Add\s+[\d.]+\s+(\S+)\s", re.I)
CART_COUNT_RE = re.compile(r"Items in cart:\s*(\d+)|(\d+)\s+unique items", re.I)


@dataclass
class Candidate:
    index: int
    name: str
    size: str
    price: float | None
    pack_grams: float | None
    unit: str
    in_stock: bool


@dataclass
class CartEntry:
    name: str
    size: str
    line_price: float | None
    quantity: float
    unit: str


def _flat(text: str) -> str:
    return " ".join(line.strip() for line in text.split("\n") if line.strip())


def is_sponsored(card_text: str) -> bool:
    # The label is split across elements on the live site ("Sp" / "onsored").
    return "sponsored" in card_text.replace("\n", "").replace(" ", "").lower()


def parse_card(index: int, text: str, name: str, add_label: str | None, item: str,
               in_cart: bool = False) -> Candidate:
    """One search-result card → Candidate. `item` is the grocery item, for 'each' sizes.

    A product already in the cart shows +/- quantity buttons instead of "Add" (seen live);
    that is in stock, not unavailable.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    price_m = PRICE_RE.search(text)
    after_name = lines[lines.index(name) + 1:] if name in lines else lines
    size = next((line for line in after_name if SIZE_LINE_RE.match(line)), "")
    about = ABOUT_RE.search(text)
    pack_grams = parse_size(size, item) if size else None
    if pack_grams is None and about:  # sold by weight: "About 0.33 lb each"
        pack_grams = float(about.group(1)) * G_PER["lb"]
        size = size or f"about {about.group(1)} lb"
    unit_m = ADD_UNIT_RE.match(add_label or "")
    return Candidate(
        index=index,
        name=name,
        size=size,
        price=float(price_m.group(1).replace(",", "")) if price_m else None,
        pack_grams=pack_grams,
        unit=unit_m.group(1) if unit_m else "ct",
        in_stock="out of stock" not in text.lower() and (add_label is not None or in_cart),
    )


def parse_quantity(card_text: str) -> float:
    m = QTY_RE.search(_flat(card_text))
    return float(m.group(1)) if m else 0.0


def parse_cart_count(label: str | None) -> int | None:
    m = CART_COUNT_RE.search(label or "")
    return int(m.group(1) or m.group(2)) if m else None


def parse_cart_line(text: str) -> CartEntry | None:
    """'Rani Foods Mixed Dal (2 lb) | $5.99 | … | Quantity: | 1 ct' → CartEntry."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None
    head = re.match(r"^(.*)\s+\(([^)]*)\)$", lines[0])
    name, size = (head.group(1), head.group(2)) if head else (lines[0], "")
    price = re.search(r"\$([\d,]+(?:\.\d+)?)", text)
    qty = QTY_RE.search(_flat(text))
    return CartEntry(
        name=name,
        size=size,
        line_price=float(price.group(1).replace(",", "")) if price else None,
        quantity=float(qty.group(1)) if qty else 1.0,
        unit=(qty.group(2) or "ct") if qty else "ct",
    )


class InstacartSite:
    """The live site, driven through one logged-in page."""

    def __init__(self, page: Page):
        self.page = page
        self.delivery_zip: str | None = None   # what Instacart says it delivers to; set when the cart opens

    async def list_stores(self) -> list[tuple[str, str]]:
        await self.page.goto(f"{BASE}/store/directory", wait_until="domcontentloaded")
        await self.page.wait_for_timeout(3000)
        rows = await self.page.eval_on_selector_all(
            "a[href*='/storefront']",
            "els => els.map(a => [a.getAttribute('href').split('?')[0].split('/')[2], a.innerText.split('\\n')[0]])",
        )
        seen, stores = set(), []
        for slug, name in rows:
            if slug and slug not in seen:
                seen.add(slug)
                stores.append((slug, name.strip()))
        return stores

    async def _results(self, store: str, query: str):
        await self.page.goto(f"{BASE}/store/{store}/s?k={quote_plus(query)}", wait_until="domcontentloaded")
        region = self.page.get_by_role("region", name=re.compile(r"^Results for"))
        cards = region.get_by_role("group", name="Product")
        try:
            await cards.first.wait_for(timeout=RESULTS_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            # No results can mean "nothing found" or "Instacart wants a human". Don't report the
            # second as the first: every item would look unavailable.
            if await captcha_present(self.page):
                raise HumanCheckError(
                    "Instacart is asking for a human check. Set BROWSER_MODE=visible, run again, and "
                    "solve it in the Chrome window.") from None
            return None
        await self.page.wait_for_timeout(800)  # let the rest of the grid render
        return cards

    async def search(self, store: str, query: str, item: str, limit: int = 8) -> list[Candidate]:
        cards = await self._results(store, query)
        if cards is None:
            return []
        out: list[Candidate] = []
        for i in range(await cards.count()):
            card = cards.nth(i)
            text = await card.inner_text()
            if is_sponsored(text):
                continue
            heading = card.locator("h3")
            if not await heading.count():
                continue
            name = (await heading.first.inner_text()).strip()
            add = card.get_by_role("button", name=re.compile(r"^Add\b", re.I))
            label = await add.first.get_attribute("aria-label") if await add.count() else None
            in_cart = await card.get_by_role("button", name=IN_CART_BUTTON_RE).count() > 0
            out.append(parse_card(len(out), text, name, label, item, in_cart=in_cart))
            if len(out) >= limit:
                break
        return out

    async def _card_for(self, store: str, query: str, product_name: str):
        cards = await self._results(store, query)
        if cards is None:
            return None
        card = cards.filter(has=self.page.locator("h3", has_text=re.compile(rf"^{re.escape(product_name)}$")))
        return card.first if await card.count() else None

    async def _card_quantity(self, card) -> float:
        collapsed = card.get_by_role("button", name=re.compile(r"^Quantity:", re.I))
        if await collapsed.count():
            return parse_quantity(await collapsed.first.get_attribute("aria-label") or "")
        return parse_quantity(await card.inner_text())

    async def add(self, store: str, query: str, product_name: str, packs: int) -> float:
        """Make the cart hold at least `packs` of the product; returns the quantity now in the cart.

        Reads the card's quantity first, so re-running never adds twice.
        """
        card = await self._card_for(store, query, product_name)
        if card is None:
            raise LookupError(f"'{product_name}' not found when searching '{query}' at {store}")
        name_re = re.escape(product_name)
        increment = card.get_by_role("button", name=re.compile(rf"^Increment quantity of {name_re}", re.I))
        for _ in range(MAX_CLICKS_PER_ITEM):
            qty = await self._card_quantity(card)
            if qty >= packs:
                return qty
            if qty == 0:
                button = card.get_by_role("button", name=re.compile(r"^Add\b", re.I))
            elif await increment.count():
                button = increment
            else:  # collapsed "Quantity: N ct. Change quantity" — open the stepper first
                button = card.get_by_role("button", name=re.compile(r"Change quantity", re.I))
            await human_pause()
            await button.first.click()
            await self.page.wait_for_timeout(1500)
        return await self._card_quantity(card)

    async def cart_count(self, store: str) -> int | None:
        await self.page.goto(f"{BASE}/store/{store}/storefront", wait_until="domcontentloaded")
        button = self.page.get_by_role("button", name=re.compile(r"View Cart", re.I)).first
        await button.wait_for(timeout=RESULTS_TIMEOUT_MS)
        return parse_cart_count(await button.get_attribute("aria-label"))

    async def _open_cart(self, store: str):
        await self.cart_count(store)
        await self.page.get_by_role("button", name=re.compile(r"View Cart", re.I)).first.click()
        await self.page.wait_for_timeout(2500)
        dialog = self.page.get_by_role("dialog").last
        zip_match = re.search(r"Shopping in (\d{5})", await dialog.inner_text())  # also shown on an empty cart
        self.delivery_zip = zip_match.group(1) if zip_match else self.delivery_zip
        return dialog

    async def cart_lines(self, store: str) -> list[CartEntry]:
        dialog = await self._open_cart(store)
        texts = await dialog.evaluate("""d => [...d.querySelectorAll('button')]
            .filter(b => /^Increment quantity of /i.test(b.getAttribute('aria-label') || ''))
            .map(b => { let n = b;
                        while (n && n !== d && !(n.innerText || '').match(/\\(.*\\)[\\s\\S]*\\$/)) n = n.parentElement;
                        return n ? n.innerText : ''; })""")
        await self.page.keyboard.press("Escape")
        return [e for t in texts if (e := parse_cart_line(t))]

    async def clear_cart(self, store: str) -> None:
        dialog = await self._open_cart(store)
        shrink = dialog.get_by_role("button", name=re.compile(r"^(Remove |Decrement quantity of )", re.I))
        for _ in range(200):
            if not await shrink.count():
                break
            await shrink.first.click()
            await self.page.wait_for_timeout(1200)
        await self.page.keyboard.press("Escape")


class LoginRequiredError(Exception):
    """Hidden mode can't sign in: a sign-in may need a CAPTCHA, which needs a visible window."""


class HumanCheckError(Exception):
    """Instacart is asking for a human check mid-run. Only a person can pass it."""


@asynccontextmanager
async def open_instacart(user_id: str, ask: Ask) -> AsyncIterator[InstacartSite]:
    """Open the user's Chrome profile, sign in through `ask` if needed, and yield the site.

    BROWSER_MODE decides whether you see the window (see config). In `auto` the shopping runs
    in a hidden window; if you aren't signed in, a visible window opens just for the sign-in
    (the login is kept in the profile), then Chrome reopens hidden. The browser then stays
    open for the whole shop step, including any pauses.
    """
    mode = get_settings().browser_mode
    headless = mode != "visible"
    session = await BrowserSession(user_id, headless=headless).start()
    try:
        if not await ensure_logged_in(session.page):
            if mode == "hidden":
                raise LoginRequiredError("You're not signed in to Instacart. Run `python scripts/login.py` "
                                         "once (it opens a window), or set BROWSER_MODE=auto.")
            if headless:  # auto: swap to a visible window for the sign-in only
                await session.close()
                session = await BrowserSession(user_id, headless=False).start()
                await login(session.page, ask)
                await session.close()
                session = await BrowserSession(user_id, headless=True).start()
                if not await ensure_logged_in(session.page):
                    raise LoginRequiredError("Signed in, but Instacart didn't keep the session. Try again.")
            else:
                await login(session.page, ask)
        yield InstacartSite(session.page)
    finally:
        await session.close()
