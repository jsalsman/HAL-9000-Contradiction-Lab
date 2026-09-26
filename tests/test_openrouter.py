"""OpenRouter client: retries, parsing, key validation, and catalog parsing."""

import asyncio

import httpx
import pytest

from hal.openrouter import (
    APP_HEADERS,
    InvalidKeyError,
    ProviderError,
    chat,
    fetch_models,
    parse_completion,
    validate_key,
)
from tests.fakes import completion, pricing_catalog

KEY = "sk-or-v1-TESTKEY"


def call(responses, body=None):
    seen, sleeps = [], []

    def handler(request):
        seen.append(request)
        status, payload = responses.pop(0)
        return httpx.Response(status, json=payload)

    async def sleep(seconds):
        sleeps.append(seconds)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await chat(
                client, KEY, body or {"model": "m", "messages": []}, timeout=5, sleep=sleep
            )

    return go, seen, sleeps


def test_retries_429_and_5xx_then_succeeds():
    go, seen, sleeps = call([(429, {}), (502, {}), (200, completion("hi"))])
    result = asyncio.run(go())
    assert result.content == "hi" and len(seen) == 3 and len(sleeps) == 2


def test_no_retry_on_400():
    go, seen, _ = call([(400, {"error": {"message": "secret body"}})])
    with pytest.raises(ProviderError) as info:
        asyncio.run(go())
    assert info.value.status == 400 and len(seen) == 1
    assert "secret body" not in str(info.value)


def test_retries_exhausted():
    go, seen, _ = call([(503, {})] * 5)
    with pytest.raises(ProviderError):
        asyncio.run(go())
    assert len(seen) == 5


def test_error_inside_200_body_retries_when_5xx():
    go, seen, _ = call([(200, {"error": {"code": 502, "message": "x"}}), (200, completion("ok"))])
    assert asyncio.run(go()).content == "ok" and len(seen) == 2


def test_headers_usage_and_key_placement():
    go, seen, _ = call([(200, completion("ok"))])
    asyncio.run(go())
    request = seen[0]
    assert request.headers["authorization"] == f"Bearer {KEY}"
    for name, value in APP_HEADERS.items():
        assert request.headers[name] == value
    assert b'"usage":{"include":true}' in request.content.replace(b" ", b"")
    assert KEY.encode() not in request.content


def test_parse_completion_fields():
    body = completion(
        "text", model="a/b", finish="length", reasoning="why", cost=0.5, reasoning_tokens=42
    )
    result = parse_completion(body)
    assert result.finish_reason == "length" and result.reasoning == "why"
    assert result.usage == {
        "prompt_tokens": 900,
        "completion_tokens": 500,
        "reasoning_tokens": 42,
        "cost": 0.5,
    }
    assert result.provider == "FakeProvider" and result.model == "a/b"
    with pytest.raises(ProviderError):
        parse_completion({"choices": []})


def test_validate_key_drops_label():
    def handler(request):
        return httpx.Response(
            200, json={"data": {"label": "sk-or-v1-abc...xyz", "limit_remaining": 3, "usage": 1}}
        )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await validate_key(client, KEY)

    facts = asyncio.run(go())
    assert "label" not in facts and facts["limit_remaining"] == 3


def test_validate_key_rejects():
    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(401, json={}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await validate_key(client, KEY)

    with pytest.raises(InvalidKeyError):
        asyncio.run(go())


def test_fetch_models_parses_prices_without_key():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=pricing_catalog())

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_models(client)

    models = asyncio.run(go())
    assert models["openai/gpt-6-sol"]["prompt"] == pytest.approx(2e-6)
    assert "authorization" not in seen[0].headers
