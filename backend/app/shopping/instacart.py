"""Instacart page adapter: every selector and every bit of page text parsing.

Deterministic Playwright only. Selectors use roles and accessible names seen
on the live site (Oct 2026):
- search results live in a region named 'Results for "<query>"';
- each product is a group with an h3 name, a "Current price: $X" line, a size line and an
  "Add 1 <unit> <name>" button. The group used to be named "Product"; since late Oct 2026 it is
  named after the product, so cards are found as groups that contain an h3. Promoted carousels
  ("slide N of M" groups) sit inside the results without "Sponsored" text and are skipped;
- right after adding, the card shows "Quantity: N <unit>" and buttons
  "Increment quantity of <name>" / "Remove <name>" / "Decrement quantity of <name>";
- on a later page load, an in-cart product shows only a collapsed
  "Quantity: N ct. Change quantity" button; clicking it reveals the +/- buttons;
- the cart opens from a "View Cart. Items in cart: N…" button as a dialog,
  one cart per store.
Ads ("Sponsored") appear inside results and are skipped.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote_plus

from playwright.async_api import Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.shopping.packs import G_PER, parse_size
from app.config import get_settings
from app.shopping.session import (
    Ask, BrowserSession, captcha_present, ensure_logged_in, human_pause, login,
)

BASE = "https://www.instacart.com"
RESULTS_TIMEOUT_MS = 15000
PRODUCT_CARD = ('[role="group"]:has(h3):not([aria-label^="slide "]):not([aria-label^="slide "] *)'
                ':not([aria-label="items carousel"] *)')
MAX_CLICKS_PER_ITEM = 12


def _escape(text: str) -> str:
    """re.escape for patterns Playwright hands to the browser as /…/ JavaScript regexes:
    a bare "/" (e.g. "95/5 Ground Chicken") would end the pattern early and break the selector."""
    return re.escape(text).replace("/", r"\/")
CLICK_TIMEOUT_MS = 8000

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
    """The live site, driven through one logged-in page, plus up to `tabs - 1` helper tabs.

    Helper tabs are opened from the same browser context, so they share the sign-in and the checkout
    guard (which is installed on the context, not on a page). They let several items be searched at once
    and keep each search open so the product can be added from it without loading the page again.
    """

    def __init__(self, page: Page, tabs: int = 1):
        self.page = page
        self.batch_size = max(1, tabs)         # items searched (and then added) together
        self._extra: list[Page] = []
        self._held: dict[tuple[str, str], Page] = {}   # (store, query) -> the tab showing those results
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

    async def _results(self, store: str, query: str, page: Page | None = None, reuse: bool = False):
        page = page or self.page
        if not reuse:
            await page.goto(f"{BASE}/store/{store}/s?k={quote_plus(query)}", wait_until="domcontentloaded")
        # Match this query's results: Instacart swaps results in place, so a bare "Results for"
        # can still be the previous search's cards for a moment.
        region = page.get_by_role("region", name=re.compile(rf"^Results for\W+{_escape(query)}\W*$", re.I))
        # Product cards are groups with an h3. Skip promoted carousels inside the results ("slide 1 of 5"
        # wrappers and anything in them): their products are ads and carry no "Sponsored" text.
        cards = region.locator(PRODUCT_CARD)
        try:
            await cards.first.wait_for(timeout=RESULTS_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            # No results can mean "nothing found" or "Instacart wants a human". Don't report the
            # second as the first: every item would look unavailable.
            if await captcha_present(page):
                raise HumanCheckError(
                    "Instacart is asking for a human check. Set BROWSER_MODE=visible, run again, and "
                    "solve it in the Chrome window.") from None
            return None
        await self._settle(cards)
        return cards

    @staticmethod
    async def _settle(cards) -> None:
        """Wait for the grid to finish rendering: stop as soon as the card count holds still, instead of
        a fixed 800 ms every time."""
        last = -1
        for _ in range(10):
            n = await cards.count()
            if n == last:
                return
            last = n
            await asyncio.sleep(0.12)

    async def _tab(self, i: int) -> Page:
        if i == 0:
            return self.page
        while len(self._extra) < i:
            self._extra.append(await self.page.context.new_page())
        return self._extra[i - 1]

    async def search_batch(self, store: str, requests: list[tuple[str, str]]) -> list[list[Candidate]]:
        """Search up to batch_size items at once, each in its own tab, and keep each tab open: add() for
        the chosen product reuses it. Results come back in request order."""
        if len(requests) > self.batch_size:
            raise ValueError(f"at most {self.batch_size} searches at a time")

        pages = [await self._tab(i) for i in range(len(requests))]    # open the tabs in order, then search together

        async def one(page: Page, query: str, item: str) -> list[Candidate]:
            found = await self.search(store, query, item, page=page)
            if found and (store, query) not in self._held:
                self._held[(store, query)] = page
            return found

        return list(await asyncio.gather(*(one(pg, q, it) for pg, (q, it) in zip(pages, requests))))

    async def close_tabs(self) -> None:
        """Close the helper tabs (the main page stays open)."""
        self._held.clear()
        for page in self._extra:
            try:
                await page.close()
            except PlaywrightError:
                pass
        self._extra.clear()

    async def search(self, store: str, query: str, item: str, limit: int = 8,
                     page: Page | None = None) -> list[Candidate]:
        cards = await self._results(store, query, page)
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

    async def _card_for(self, store: str, query: str, product_name: str, page: Page | None = None):
        """The product's card: from the tab that searched for it if there is one, else a fresh search."""
        for held in ([page] if page is not None else []) + [None]:
            cards = await self._results(store, query, held, reuse=held is not None)
            if cards is None:
                continue
            card = cards.filter(has=(held or self.page).locator("h3", has_text=re.compile(rf"^{_escape(product_name)}$")))
            if await card.count():
                return card.first, (held or self.page)
        return None, self.page

    async def _card_quantity(self, card) -> float:
        collapsed = card.get_by_role("button", name=re.compile(r"^Quantity:", re.I))
        if await collapsed.count():
            return parse_quantity(await collapsed.first.get_attribute("aria-label") or "")
        return parse_quantity(await card.inner_text())

    async def _dismiss_overlays(self, page: Page | None = None) -> None:
        """Close a pop-up Instacart opened over the results (seen Nov 2026: an "iap-modal" after adding
        an item). Left open, it swallows every click. Escape first, then the pop-up's own close button."""
        page = page or self.page
        overlay = page.locator('[data-dialog-ref], [role="dialog"][aria-modal="true"]')
        for _ in range(3):
            if not await overlay.count() or not await overlay.first.is_visible():
                return
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            if await overlay.count() and await overlay.first.is_visible():
                close = overlay.first.get_by_role("button", name=re.compile(r"close|dismiss|no thanks|not now|got it", re.I))
                if await close.count():
                    await close.first.click(timeout=3000)
                    await page.wait_for_timeout(400)

    async def add(self, store: str, query: str, product_name: str, packs: int) -> float:
        """Like _add, but a page error on one item becomes a LookupError (noted on the cart), not a stopped run."""
        try:
            return await self._add(store, query, product_name, packs)
        except LookupError:
            raise
        except PlaywrightError as e:
            raise LookupError(f"Couldn't add {product_name} at {store}: {str(e).splitlines()[0][:160]}. "
                              "Add it yourself in Instacart.") from e

    async def _add(self, store: str, query: str, product_name: str, packs: int) -> float:
        """Make the cart hold at least `packs` of the product; returns the quantity now in the cart.

        Reads the card's quantity first, so re-running never adds twice. A click Instacart keeps
        blocking becomes a LookupError, so one stubborn item is noted on the cart instead of
        stopping the whole run.
        """
        card, page = await self._card_for(store, query, product_name, self._held.pop((store, query), None))
        if card is None:
            raise LookupError(f"'{product_name}' not found when searching '{query}' at {store}")
        name_re = _escape(product_name)
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
            await human_pause(0.1, 0.3)
            await self._dismiss_overlays(page)
            try:
                await button.first.click(timeout=CLICK_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                await self._dismiss_overlays(page)
                try:
                    await button.first.click(timeout=CLICK_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    raise LookupError(f"Couldn't add {product_name} at {store}: Instacart's page kept blocking "
                                      f"the click. Add it yourself in Instacart.") from None
            await self._wait_changed(card, qty)
        return await self._card_quantity(card)

    async def _wait_changed(self, card, before: float, limit_s: float = 3.0) -> None:
        """After a click, wait only until the card shows a new quantity (not a fixed 1.5 s)."""
        for _ in range(int(limit_s / 0.12)):
            if await self._card_quantity(card) != before:
                return
            await asyncio.sleep(0.12)

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
        # Hidden Chrome searches three items at once in separate tabs. A visible window stays on one tab:
        # background tabs in a visible window can be throttled by the browser.
        yield InstacartSite(session.page, tabs=3 if headless else 1)
    finally:
        await session.close()
