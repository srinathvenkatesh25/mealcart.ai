"""Search results read from a trimmed copy of the live Instacart page (real headless Chromium, no network).

In late Oct 2026 Instacart stopped naming product cards "Product" and started naming each card after
the product. The old selector found nothing, so every search waited out its timeout and returned no
results. This test pins the current page shape.
"""

from pathlib import Path

from playwright.async_api import async_playwright

from app.shopping.instacart import BASE, InstacartSite

FIXTURE = Path(__file__).parent / "fixtures" / "instacart_search_banana.html"


async def test_search_reads_cards_named_after_the_product():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.route(f"{BASE}/**", lambda route: route.fulfill(body=FIXTURE.read_text(), content_type="text/html; charset=utf-8"))
        found = await InstacartSite(page).search("kroger", "banana", "banana")
        await browser.close()

    assert [c.name for c in found][:3] == [
        "Fresh Bunch of Bananas – 5-7 Bananas",
        "Fresh Bunch of Organic Bananas – 5-7 Bananas",
        "Fresh Plantain - Single",
    ]
    assert found[0].price == 1.77
    assert found[0].in_stock


async def test_search_skips_promoted_carousels_without_sponsored_text():
    # The egg page opens with a promoted "slide 1 of 5" carousel (Eggland's Best, even wine) that has
    # no "Sponsored" label. Only the real results should come back.
    fixture = Path(__file__).parent / "fixtures" / "instacart_search_egg_with_ad.html"
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.route(f"{BASE}/**", lambda route: route.fulfill(body=fixture.read_text(), content_type="text/html; charset=utf-8"))
        found = await InstacartSite(page).search("kroger", "egg", "egg")
        await browser.close()

    assert found[0].name == "Kroger® Grade A Large White Eggs"
    assert found[0].price == 1.59
    assert not any(c.name.startswith(("Eggland", "Meiomi")) for c in found)


def _page_with_popup(closable: bool) -> str:
    """A results card whose Add opens a full-screen pop-up that swallows clicks, like Instacart's "iap-modal"."""
    close = '<button aria-label="Close" onclick="this.parentElement.remove()">×</button>' if closable else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body><main>
<div role="region" aria-label='Results for "tomato"'>
  <div role="group" aria-label="Tomato 5X6"><h3>Tomato 5X6</h3><div>Current price: $0.89</div><div id="q"></div>
    <button id="add" aria-label="Add 1 item Tomato 5X6">Add</button></div>
</div>
<script>
let qty = 0;
function render() {{
  document.getElementById("q").textContent = qty ? "Quantity: " + qty + " item" : "";
  const add = document.getElementById("add");
  add.setAttribute("aria-label", qty ? "Increment quantity of Tomato 5X6, Quantity: " + qty + " item" : "Add 1 item Tomato 5X6");
  add.textContent = qty ? "+" : "Add";
}}
document.getElementById("add").onclick = () => {{
  qty += 1; render();
  if (qty === 1) {{
    const m = document.createElement("div");
    m.setAttribute("data-dialog-ref", "iap-modal");
    m.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:9";
    m.innerHTML = `{close}`;
    document.body.appendChild(m);
  }}
}};
</script></main></body></html>"""


async def _add_tomatoes(html: str, packs: int):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.route(f"{BASE}/**", lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
        try:
            return await InstacartSite(page).add("kroger", "tomato", "Tomato 5X6", packs)
        finally:
            await browser.close()


async def test_add_closes_a_popup_that_blocks_the_next_click():
    assert await _add_tomatoes(_page_with_popup(closable=True), packs=2) == 2


async def test_add_gives_up_on_a_popup_it_cannot_close_without_stopping_the_run(monkeypatch):
    import pytest
    from app.shopping import instacart
    monkeypatch.setattr(instacart, "CLICK_TIMEOUT_MS", 500)
    with pytest.raises(LookupError, match="Add it yourself"):
        await _add_tomatoes(_page_with_popup(closable=False), packs=2)


async def test_add_works_for_a_product_name_with_a_slash():
    # "95/5" once broke the selector (a bare "/" ends a JavaScript regex) and stopped the whole run.
    name = "True Goodness by Meijer 95/5 Ground Chicken Breast"
    html = _page_with_popup(closable=True).replace("Tomato 5X6", name)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.route(f"{BASE}/**", lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
        try:
            assert await InstacartSite(page).add("meijer", "tomato", name, 2) == 2
        finally:
            await browser.close()


def _results_page(query: str, product: str) -> str:
    """A results page for one query with a single product whose Add button counts up."""
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body><main>
<div role="region" aria-label='Results for "{query}"'>
  <div role="group" aria-label="{product}"><h3>{product}</h3><div>Current price: $1.00</div><div id="q"></div>
    <button id="add" aria-label="Add 1 item {product}">Add</button></div>
</div>
<script>
let qty = 0;
document.getElementById("add").onclick = () => {{
  qty += 1;
  document.getElementById("q").textContent = "Quantity: " + qty + " item";
  const b = document.getElementById("add");
  b.setAttribute("aria-label", "Increment quantity of {product}, Quantity: " + qty + " item");
}};
</script></main></body></html>"""


async def test_several_items_are_searched_at_once_and_added_from_the_tab_that_found_them():
    import asyncio, time
    from urllib.parse import parse_qs, urlsplit
    products = {"tomato": "Tomato 5X6", "egg": "Large Eggs 12 ct", "banana": "Bananas 3 lb"}
    loads: list[str] = []

    async def handler(route):
        query = parse_qs(urlsplit(route.request.url).query)["k"][0]
        loads.append(query)
        await asyncio.sleep(0.5)          # every page takes half a second to arrive
        await route.fulfill(body=_results_page(query, products[query]), content_type="text/html; charset=utf-8")

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await context.route(f"{BASE}/**", handler)
        site = InstacartSite(page, tabs=3)

        started = time.monotonic()
        found = await site.search_batch("kroger", [(q, q) for q in products])
        searched_in = time.monotonic() - started
        assert [f[0].name for f in found] == list(products.values())
        assert len(context.pages) == 3                      # the main page plus two helper tabs
        assert searched_in < 1.2                            # three half-second pages, loaded together

        quantities = [await site.add("kroger", q, products[q], 2) for q in products]
        assert quantities == [2, 2, 2]
        assert sorted(loads) == sorted(products)            # adding did not load any page a second time

        await site.close_tabs()
        assert len(context.pages) == 1
        await browser.close()


async def test_adding_without_a_held_tab_searches_again_as_before():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        await context.route(f"{BASE}/**", lambda r: r.fulfill(body=_results_page("tomato", "Tomato 5X6"),
                                                               content_type="text/html; charset=utf-8"))
        assert await InstacartSite(page).add("kroger", "tomato", "Tomato 5X6", 1) == 1
        await browser.close()


async def test_helper_tabs_are_covered_by_the_checkout_guard():
    # The guard is installed on the browser context, so a tab opened later is blocked from checkout too.
    from app.shopping import guard
    blocked: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        await guard.install(context, blocked)
        page = await context.new_page()
        helper = await context.new_page()                   # the kind of tab search_batch opens
        for tab in (page, helper):
            try:
                await tab.goto("https://www.instacart.com/store/checkout")
            except Exception:
                pass
        await browser.close()
    assert len(blocked) == 2 and all("checkout" in b for b in blocked)
