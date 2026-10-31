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
