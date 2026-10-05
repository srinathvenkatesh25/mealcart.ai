import pytest

from app.shopping.guard import is_blocked


@pytest.mark.parametrize(
    "url, body",
    [
        ("https://www.instacart.com/store/checkout", None),
        ("https://www.instacart.com/store/checkout_v3?x=1", None),
        ("https://www.instacart.com/store/orders/123", None),
        ("https://www.instacart.com/store/account/payments", None),
        ("https://www.instacart.com/graphql", '{"operationName": "PlaceOrderMutation"}'),
        ("https://www.instacart.com/graphql", '[{"operationName": "CreateCheckoutSession"}]'),
        ("https://www.instacart.com/graphql?operationName=CheckoutV3Data&variables={}", None),
    ],
)
def test_blocks_checkout_orders_payment(url, body):
    assert is_blocked(url, body) is not None


@pytest.mark.parametrize(
    "url, body",
    [
        ("https://www.instacart.com/store/aldi/storefront", None),
        ("https://www.instacart.com/store/s?k=paneer", None),
        ("https://www.instacart.com/graphql", '{"operationName": "UpdateCartItemsMutation"}'),
        ("https://www.instacart.com/graphql?operationName=SearchResultsPlacements", None),
        ("https://www.instacart.com/v3/events?name=checkout_viewed", None),  # analytics query only
        ("https://www.instacart.com/graphql", "not json"),
    ],
)
def test_allows_shopping_and_cart(url, body):
    assert is_blocked(url, body) is None
