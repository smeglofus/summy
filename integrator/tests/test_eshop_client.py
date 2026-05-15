import pytest
import requests
import responses

from integrator.eshop_client import EshopClient, EshopError
from integrator.rate_limiter import RateLimiter


def _client(**kwargs):
    """Builds an EshopClient that never actually sleeps."""
    sleep_calls: list[float] = []
    limiter = RateLimiter(
        rate=1000, per=1.0,
        sleep_fn=lambda s: sleep_calls.append(s),
        monotonic_fn=lambda: 0.0,
    )
    client = EshopClient(
        base_url="https://api.fake-eshop.cz/v1",
        api_key="symma-secret-token",
        rate_limiter=limiter,
        max_retries=4,
        timeout=1.0,
        sleep_fn=lambda s: sleep_calls.append(s),
        **kwargs,
    )
    return client, sleep_calls


@responses.activate
def test_create_product_sends_post_with_auth_header():
    responses.post(
        "https://api.fake-eshop.cz/v1/products/",
        json={"ok": True}, status=201,
    )
    client, _ = _client()
    response = client.create_product({"sku": "SKU-1"})
    assert response.status_code == 201
    assert responses.calls[0].request.headers["X-Api-Key"] == "symma-secret-token"


@responses.activate
def test_update_product_sends_patch_with_sku_in_url():
    responses.patch(
        "https://api.fake-eshop.cz/v1/products/SKU-2/",
        json={"ok": True}, status=200,
    )
    client, _ = _client()
    response = client.update_product("SKU-2", {"price_with_vat": "121.00"})
    assert response.status_code == 200


@responses.activate
def test_429_retries_and_eventually_succeeds():
    responses.post("https://api.fake-eshop.cz/v1/products/",
                   json={"err": "rate"}, status=429,
                   headers={"Retry-After": "2"})
    responses.post("https://api.fake-eshop.cz/v1/products/",
                   json={"ok": True}, status=201)

    client, sleeps = _client()
    response = client.create_product({"sku": "SKU-3"})
    assert response.status_code == 201
    assert 2.0 in sleeps  # respected Retry-After


@responses.activate
def test_429_without_retry_after_uses_backoff():
    responses.post("https://api.fake-eshop.cz/v1/products/", status=429)
    responses.post("https://api.fake-eshop.cz/v1/products/", status=201, json={"ok": True})

    client, sleeps = _client()
    client.create_product({"sku": "SKU-4"})
    # backoff for attempt 0 = 2**0 * 0.5 = 0.5s
    assert 0.5 in sleeps


@responses.activate
def test_5xx_retries_and_eventually_succeeds():
    responses.post("https://api.fake-eshop.cz/v1/products/", status=503)
    responses.post("https://api.fake-eshop.cz/v1/products/", status=201, json={"ok": True})

    client, _ = _client()
    response = client.create_product({"sku": "SKU-5"})
    assert response.status_code == 201


@responses.activate
def test_request_exception_retries_and_eventually_succeeds():
    responses.post(
        "https://api.fake-eshop.cz/v1/products/",
        body=requests.ConnectionError("boom"),
    )
    responses.post("https://api.fake-eshop.cz/v1/products/", status=201, json={"ok": True})

    client, sleeps = _client()
    response = client.create_product({"sku": "SKU-5A"})
    assert response.status_code == 201
    assert 0.5 in sleeps


@responses.activate
def test_request_exception_exhausts_retries_and_raises():
    for _ in range(10):
        responses.post(
            "https://api.fake-eshop.cz/v1/products/",
            body=requests.ConnectionError("network down"),
        )

    client, _ = _client()
    with pytest.raises(EshopError, match="network error"):
        client.create_product({"sku": "SKU-5B"})
    assert len(responses.calls) == client.max_retries + 1


@responses.activate
def test_4xx_other_than_429_is_not_retried():
    responses.post("https://api.fake-eshop.cz/v1/products/", status=400, json={"err": "bad"})

    client, _ = _client()
    with pytest.raises(EshopError, match="400"):
        client.create_product({"sku": "SKU-6"})
    assert len(responses.calls) == 1  # no retry


@responses.activate
def test_max_retries_exceeded_raises():
    for _ in range(10):
        responses.post("https://api.fake-eshop.cz/v1/products/", status=429)

    client, _ = _client()
    with pytest.raises(EshopError, match="exhausted retries"):
        client.create_product({"sku": "SKU-7"})
    assert len(responses.calls) == client.max_retries + 1


@responses.activate
def test_create_product_sends_idempotency_key_when_provided():
    responses.post("https://api.fake-eshop.cz/v1/products/", json={"ok": True}, status=201)

    client, _ = _client()
    client.create_product({"sku": "SKU-8"}, idempotency_key="hash-abc")
    assert responses.calls[0].request.headers["Idempotency-Key"] == "hash-abc"


@responses.activate
def test_create_product_omits_idempotency_key_header_when_not_provided():
    responses.post("https://api.fake-eshop.cz/v1/products/", json={"ok": True}, status=201)

    client, _ = _client()
    client.create_product({"sku": "SKU-9"})
    assert "Idempotency-Key" not in responses.calls[0].request.headers
