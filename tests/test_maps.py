import pytest

from app.nutrition.maps import allergen_hit, contains_term, dislike_hit, restriction_hit


@pytest.mark.parametrize(
    "ingredient, term, expected",
    [
        ("egg_whites", "egg", True),
        ("eggplant", "egg", False),
        ("button_mushrooms", "mushrooms", True),
        ("roma_tomatoes", "tomato", True),
        ("atta_whole_wheat", "wheat", True),
        ("buckwheat_groats", "wheat", False),
    ],
)
def test_contains_term(ingredient, term, expected):
    assert contains_term(ingredient, term) is expected


@pytest.mark.parametrize(
    "ingredient, allergy, hit",
    [
        ("ghee", "dairy", True),
        ("paneer", "Dairy", True),
        ("coconut_milk", "dairy", False),
        ("peanut_butter", "dairy", False),
        ("peanut_butter", "peanuts", True),
        ("cashew_nuts", "tree nuts", True),
        ("whole_wheat_bread", "gluten", True),
        ("kiwi", "kiwi", True),  # unknown allergen matches its own name
    ],
)
def test_allergen_hit(ingredient, allergy, hit):
    assert (allergen_hit(ingredient, allergy) is not None) is hit


@pytest.mark.parametrize(
    "ingredient, restriction, hit",
    [
        ("chicken_thigh_boneless_skinless", "vegetarian", True),
        ("salmon_fillet", "pescatarian", False),
        ("greek_yogurt_nonfat", "vegan", True),
        ("bacon", "halal", True),
        ("ground_beef", "no_beef", True),
        ("lamb_shoulder", "no_lamb", True),  # unknown "no_x" blocks x
        ("chickpeas_dry", "vegetarian", False),
    ],
)
def test_restriction_hit(ingredient, restriction, hit):
    assert (restriction_hit(ingredient, restriction) is not None) is hit


def test_dislike_synonyms():
    assert dislike_hit("light_mayo", "mayonnaise")
    assert dislike_hit("shiitake", "Mushrooms")
    assert dislike_hit("onion", "mushrooms") is None
