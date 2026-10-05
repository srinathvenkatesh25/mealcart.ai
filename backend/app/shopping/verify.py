"""Phase C: check the cart, as Instacart shows it, against the grocery list."""

from app.models import CartLine, CartReport, CoverageRow, GroceryList
from app.shopping.instacart import CartEntry

# Pack-size rounding: a 2 lb bag for 0.9 kg needed is fine, 1% short is not worth failing.
COVERAGE_SLACK = 0.98


def build_report(grocery_list: GroceryList, carts: dict[str, list[CartEntry]],
                 chosen: dict[str, tuple[str, str, float, str | None]], budget: float | None,
                 notes: list[str]) -> tuple[CartReport, bool]:
    """`carts` maps store name → its cart as Instacart shows it; `chosen` maps grocery item →
    (store name, product name, pack grams, substituted_for).

    Returns the report and whether every item is covered. Cart lines that match no item
    on the list are reported in notes (e.g. kept from before the run).
    """
    lines, coverage = [], []
    for item in grocery_list.items:
        pick = chosen.get(item.name)
        entry = next((e for e in carts.get(pick[0], []) if e.name == pick[1]), None) if pick else None
        grams = entry.quantity * pick[2] if entry and pick[2] else 0.0
        covered = entry is not None and (grams >= item.total_grams * COVERAGE_SLACK or not pick[2])
        coverage.append(CoverageRow(item=item.name, grams_needed=item.total_grams, grams_in_cart=round(grams, 1),
                                    covered=covered, store=pick[0] if entry else ""))
        if entry:
            line_total = entry.line_price or 0.0
            lines.append(CartLine(
                product_name=entry.name, pack_size=entry.size, pack_grams=pick[2] or 0.0,
                packs=int(entry.quantity), unit_price_usd=round(line_total / max(entry.quantity, 1), 2),
                line_total_usd=line_total, grocery_item=item.name, substituted_for=pick[3], store=pick[0],
            ))
    for store, cart in carts.items():
        matched = {line.product_name for line in lines if line.store == store}
        notes = notes + [f"Also in your {store} cart (not on the list): {e.name}" for e in cart if e.name not in matched]
    subtotals = {store: round(sum(e.line_price or 0.0 for e in cart), 2) for store, cart in carts.items()}
    subtotal = round(sum(subtotals.values()), 2)
    over = round(subtotal - budget, 2) if budget is not None and subtotal > budget else None
    report = CartReport(store=" + ".join(carts), stores=list(carts), subtotals=subtotals, lines=lines,
                        coverage=coverage, subtotal_usd=subtotal, over_budget_by_usd=over, notes=notes)
    return report, all(c.covered for c in coverage)
