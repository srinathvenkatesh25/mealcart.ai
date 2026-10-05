import pytest

from app.shopping.packs import packs_needed, parse_size, target_qty


@pytest.mark.parametrize(
    "text, item, grams",
    [
        ("1.5 lb", "chicken_breast", 680.4),
        ("16 oz", "basmati_rice", 453.6),
        ("2 lbs", "onion", 907.2),
        ("1 kg", "atta_whole_wheat", 1000.0),
        ("500 g", "paneer", 500.0),
        ("2 x 8 oz", "paneer", 453.6),
        ("48 fl oz", "canola_oil", 1305.9),       # 1419.5 ml x 0.92 g/ml
        ("1 gal", "milk_2_percent", 3899.0),       # 3785 ml x 1.03
        ("Half Gallon", "milk_2_percent", 1949.5),
        ("12 ct", "eggs", 600.0),
        ("18 count", "egg", 900.0),
        ("1 dozen", "eggs", 600.0),
        ("each", "onion", 150.0),
        ("1 bunch", "cilantro", 60.0),
        ("per lb", "tomato", 453.6),
        ("1/2 lb", "green_chili", 226.8),
    ],
)
def test_parse_size(text, item, grams):
    assert parse_size(text, item) == pytest.approx(grams, abs=1)


@pytest.mark.parametrize("text, item", [("each", "chicken_breast"), ("family size", "rice"), ("12 ct", "paneer")])
def test_parse_size_unknown_returns_none(text, item):
    assert parse_size(text, item) is None


def test_packs_needed_rounds_up():
    assert packs_needed(1100, 680.4) == 2
    assert packs_needed(680.4, 680.4) == 1
    assert packs_needed(5, 1000) == 1


@pytest.mark.parametrize(
    "item, grams, expected",
    [
        ("turmeric_ground", 14, "1 small pack"),
        ("eggs", 740, "15 eggs"),
        ("onion", 980, "7 × onion (≈ 2.2 lb)"),
        ("chicken_breast_boneless_skinless", 1820, "≈ 4.25 lb"),
        ("canola_oil", 210, "≈ 8 fl oz"),
        ("milk_2_percent", 830, "≈ 28 fl oz"),
        ("peanut_butter", 120, "≈ 5 oz"),       # "butter", not oil or milk
        ("coconut_milk", 400, "≈ 14 fl oz"),
    ],
)
def test_target_qty(item, grams, expected):
    assert target_qty(item, grams) == expected
