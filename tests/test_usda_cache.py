import json

import httpx
import pytest

from app import db
from app.nutrition.usda import USDAClient, UnknownIngredientError


def food(fdc_id, description, kcal, protein, carbs=0.0, fat=0.0, energy_id=1008):
    return {
        "fdcId": fdc_id,
        "description": description,
        "foodNutrients": [
            {"nutrientId": energy_id, "unitName": "KCAL", "value": kcal},
            {"nutrientId": 1062, "unitName": "kJ", "value": kcal * 4.184},
            {"nutrientId": 1003, "unitName": "G", "value": protein},
            {"nutrientId": 1005, "unitName": "G", "value": carbs},
            {"nutrientId": 1004, "unitName": "G", "value": fat},
        ],
    }


class FakeFDC:
    """Answers each query from a dict; records every request body."""

    def __init__(self, responses: dict[str, list[dict]], statuses: list[int] | None = None):
        self.responses = responses
        self.statuses = list(statuses or [])
        self.queries: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        self.queries.append(query)
        if self.statuses:
            return httpx.Response(self.statuses.pop(0))
        return httpx.Response(200, json={"foods": self.responses.get(query, [])})


async def no_sleep(_):
    return None


def client(fake: FakeFDC) -> USDAClient:
    return USDAClient(api_key="k", http=httpx.AsyncClient(transport=httpx.MockTransport(fake)), sleep=no_sleep)


@pytest.fixture(autouse=True)
async def _db():
    await db.init_db()


async def test_cache_miss_then_hit():
    fake = FakeFDC({"broccoli raw": [food(1, "Broccoli, raw", 34, 2.8)]})
    usda = client(fake)
    first = await usda.get_macros_per_100g("Broccoli")
    second = await usda.get_macros_per_100g("broccoli")
    assert first == second and first.calories == 34
    assert fake.queries == ["broccoli raw"]  # second call served from cache


async def test_prefers_raw_over_cooked():
    fake = FakeFDC({
        "quinoa raw": [
            food(1, "Quinoa, cooked", 120, 4.4),
            food(2, "Quinoa, uncooked, raw", 368, 14.1),
        ]
    })
    macros = await client(fake).get_macros_per_100g("quinoa")
    assert macros.fdc_id == 2 and macros.calories == 368


async def test_raw_is_matched_as_a_word():
    fake = FakeFDC({
        "strawberry raw": [
            food(1, "Strawberries, frozen, sweetened", 78, 0.5),
            food(2, "Strawberries, raw", 32, 0.7),
        ]
    })
    assert (await client(fake).get_macros_per_100g("strawberry")).fdc_id == 2


async def test_energy_falls_back_to_atwater():
    fake = FakeFDC({"kale raw": [food(7, "Kale, raw", 43, 2.9, energy_id=2047)]})
    assert (await client(fake).get_macros_per_100g("kale")).calories == 43


async def test_alias_query_used():
    fake = FakeFDC({"butter oil anhydrous": [food(3, "Butter oil, anhydrous", 876, 0.3, fat=99.5)]})
    macros = await client(fake).get_macros_per_100g("ghee")
    assert fake.queries == ["butter oil anhydrous"] and macros.fat_g == 99.5


async def test_falls_back_to_query_without_raw():
    fake = FakeFDC({"turmeric ground": [food(4, "Spices, turmeric, ground", 312, 9.7)]})
    macros = await client(fake).get_macros_per_100g("turmeric_ground")
    assert fake.queries == ["turmeric ground raw", "turmeric ground"]
    assert macros.fdc_id == 4


async def test_unknown_ingredient_raises_and_is_not_cached():
    fake = FakeFDC({})
    with pytest.raises(UnknownIngredientError):
        await client(fake).get_macros_per_100g("unobtainium_paste")
    assert await db.get_usda_cache("unobtainium_paste") is None


async def test_retries_on_429_then_succeeds():
    fake = FakeFDC({"apple raw": [food(5, "Apples, raw, with skin", 52, 0.3)]}, statuses=[429, 503])
    macros = await client(fake).get_macros_per_100g("apple")
    assert macros.fdc_id == 5 and len(fake.queries) == 3


async def test_gives_up_after_retries():
    fake = FakeFDC({}, statuses=[500, 500, 500])
    with pytest.raises(httpx.HTTPStatusError):
        await client(fake).get_macros_per_100g("apple")


async def test_result_must_name_the_food():
    # Live USDA returned "Bison, ground, raw" for "turmeric ground raw": "ground" alone must not match.
    fake = FakeFDC({
        "turmeric ground raw": [food(1, "Bison, ground, raw", 146, 20.2)],
        "turmeric ground": [food(2, "Spices, turmeric, ground", 312, 9.7)],
    })
    assert (await client(fake).get_macros_per_100g("turmeric_ground")).fdc_id == 2


async def test_more_matching_words_beats_raw():
    fake = FakeFDC({
        "turkey breast raw": [
            food(1, "Turkey, whole, meat only, raw", 114, 23.6),
            food(2, "Turkey, breast, meat only, raw", 111, 24.6),
        ]
    })
    assert (await client(fake).get_macros_per_100g("turkey breast")).fdc_id == 2


async def test_manual_macros_skip_the_api():
    fake = FakeFDC({})
    macros = await client(fake).get_macros_per_100g("Paneer")
    assert macros.calories == 321 and macros.protein_g == 25.0
    assert fake.queries == []


async def test_head_name_match_wins():
    fake = FakeFDC({
        "apple raw": [food(1, "Rose-apples, raw", 25, 0.6), food(2, "Apples, raw, with skin", 52, 0.3)],
        "sweet potato raw": [
            food(3, "Sweet potato leaves, raw", 42, 2.5),
            food(4, "Sweet potato, raw, unprepared", 86, 1.6),
        ],
    })
    usda = client(fake)
    assert (await usda.get_macros_per_100g("apple")).fdc_id == 2
    assert (await usda.get_macros_per_100g("sweet_potato")).fdc_id == 4
