"""Card and cart text below is copied from the live site (Oct 2026)."""

import pytest

from app.shopping.instacart import (
    is_sponsored, parse_card, parse_cart_count, parse_cart_line, parse_quantity,
)

TOOR = "Best seller\nCurrent price: $5.99\n$599\nRani Toor Dal\n32 oz\nAdd"
CHICKEN = ("Best seller\nCurrent price: $10.88 per package (estimated)\n$1088\n\n/pkg (est.)\n\n"
           "Markets of Meijer Boneless 100% All Natural Chicken Breast\n$7.99 / lb\nAbout 1.36 lb\nAdd")
BANANA = ("Best seller\nCurrent price: $0.16 each (estimated)\n$016\n\neach (est.)\n\nBananas, per lb\n"
          "$0.49 / lb\nAbout 0.33 lb each\nMany in stock\nAdd")
PANEER_AD = ("Vegetarian\nCurrent price: $5.12\n$512\nSpend $30, save $5\nDeep Paneer Tikka Masala\n9 oz\n"
             "See eligible items\nSp\nonsored\nAdd")
RANCH = ("Current price: $5.99\n$599\nBuy 2 Save $0.75\nHidden Valley Original Ranch Dressing\n★★★★★\n★★★★★\n"
         "(9.34K)\n24 fl oz\nApply coupon\nAdd")


def test_plain_card():
    c = parse_card(0, TOOR, "Rani Toor Dal", "Add 1 ct Rani Toor Dal", "toor_dal")
    assert (c.name, c.size, c.price, c.unit, c.in_stock) == ("Rani Toor Dal", "32 oz", 5.99, "ct", True)
    assert c.pack_grams == pytest.approx(907.2, abs=0.1)


def test_weight_priced_package():
    c = parse_card(1, CHICKEN, "Markets of Meijer Boneless 100% All Natural Chicken Breast",
                   "Add 1 pkg Markets of Meijer Boneless 100% All Natural Chicken Breast", "chicken_breast")
    assert c.price == 10.88 and c.unit == "pkg"
    assert c.pack_grams == pytest.approx(1.36 * 453.592, abs=0.1) and c.size == "about 1.36 lb"


def test_each_with_about_weight():
    c = parse_card(2, BANANA, "Bananas, per lb", "Add 1 ct Bananas, per lb", "banana")
    assert c.price == 0.16 and c.pack_grams == pytest.approx(0.33 * 453.592, abs=0.1)


def test_ratings_lines_skipped_for_size():
    c = parse_card(3, RANCH, "Hidden Valley Original Ranch Dressing", "Add 1 ct …", "ranch")
    assert c.size == "24 fl oz"


def test_sponsored_detected_across_split_label():
    assert is_sponsored(PANEER_AD) and not is_sponsored(TOOR)


def test_no_add_button_means_not_in_stock():
    assert not parse_card(0, TOOR, "Rani Toor Dal", None, "toor_dal").in_stock
    assert not parse_card(0, TOOR + "\nOut of stock", "Rani Toor Dal", "Add 1 ct x", "toor_dal").in_stock


def test_quantity_and_cart_count():
    after_add = "Best seller\nCurrent price: $5.99\n$599\nRani Foods Mixed Dal\n2 lb\nQuantity:\n1 ct"
    assert parse_quantity(after_add) == 1 and parse_quantity(TOOR) == 0
    assert parse_cart_count("View Cart. Items in cart: 1, Add $4.01 to get $0 delivery fee") == 1
    assert parse_cart_count("View Cart. 0 unique items in your cart") == 0
    assert parse_cart_count(None) is None


def test_cart_line():
    e = parse_cart_line("Rani Foods Mixed Dal (2 lb)\n$5.99\nReplace with best match\nQuantity:\n1 ct")
    assert (e.name, e.size, e.line_price, e.quantity, e.unit) == ("Rani Foods Mixed Dal", "2 lb", 5.99, 1.0, "ct")


def test_card_already_in_cart_is_in_stock():
    # Live, on a later page load: no Add button, just "Quantity: 1 ct. Change quantity".
    text = "No preservatives\nCurrent price: $8.05\n$805\nDESI NATURAL PANEER\n340 g\n1 ct"
    c = parse_card(0, text, "DESI NATURAL PANEER", None, "paneer", in_cart=True)
    assert c.in_stock and c.pack_grams == 340 and c.size == "340 g"


def test_quantity_from_collapsed_button_label():
    assert parse_quantity("Quantity: 1 ct. Change quantity") == 1
    assert parse_quantity("Quantity: 2.5 lb. Change quantity") == 2.5
