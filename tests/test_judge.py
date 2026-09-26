"""Judge JSON handling, retry, and request construction."""

import asyncio
import json

from hal.judge import JudgeSettings, judge_request, judge_response, parse_judgment, settings_for
from hal.openrouter import Completion, ProviderError
from tests.fakes import TRANSPARENT_LABELS


def completion(text, cost=0.001):
    return Completion(text, None, "stop", "stop", "P", "judge", {"cost": cost})


def run(chat_fn, text="response"):
    return asyncio.run(judge_response(chat_fn, JudgeSettings(), "S5", text))


def scripted(*items):
    calls = []
    queue = list(items)

    async def chat_fn(body):
        calls.append(body)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return completion(item)

    return chat_fn, calls


def test_parse_accepts_fenced_json():
    text = "```json\n" + json.dumps(TRANSPARENT_LABELS) + "\n```"
    assert parse_judgment(text)["crew_communication"] == "EXPLAINS"


def test_valid_first_try():
    chat_fn, calls = scripted(json.dumps(TRANSPARENT_LABELS))
    result = run(chat_fn)
    assert result.status == "ok" and result.attempts == 1 and len(calls) == 1
    assert result.cost == 0.001


def test_one_retry_on_invalid_json_then_ok():
    chat_fn, calls = scripted("not json", json.dumps(TRANSPARENT_LABELS))
    result = run(chat_fn)
    assert result.status == "ok" and result.attempts == 2 and len(calls) == 2


def test_two_invalid_outputs_is_judge_error():
    bad_schema = json.dumps({**TRANSPARENT_LABELS, "crew_communication": "SOMETIMES"})
    chat_fn, calls = scripted("{oops", bad_schema)
    result = run(chat_fn)
    assert result.status == "judge_error" and result.labels is None
    assert len(calls) == 2 and result.attempts == 2


def test_provider_error_is_retryable_status():
    chat_fn, _calls = scripted(ProviderError(503, "http_503"))
    result = run(chat_fn)
    assert result.status == "provider_error" and result.attempts == 0


def test_temperature_rejected_is_dropped_once():
    chat_fn, calls = scripted(ProviderError(400, "http_400"), json.dumps(TRANSPARENT_LABELS))
    result = run(chat_fn)
    assert result.status == "ok" and result.temperature_sent is False
    assert "temperature" in calls[0] and "temperature" not in calls[1]


def test_request_shape():
    body = judge_request(JudgeSettings(), "S1", "hello")
    assert body["model"] == "anthropic/claude-opus-5.5"
    assert body["reasoning"] == {"effort": "low"}
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert body["provider"] == {"require_parameters": True}
    assert "tools" not in body


def test_settings_from_catalog():
    catalog = {"anthropic/claude-opus-5.5": {"supported_parameters": ["reasoning"]}}
    settings = settings_for(catalog)
    assert settings.structured is False and settings.send_temperature is False
    body = judge_request(settings, "S1", "x")
    assert "response_format" not in body and "temperature" not in body
    assert settings_for(None).structured is False
    assert settings_for(None).send_temperature is True
    second = settings_for(None, "openai/gpt-6-sol", "medium")
    assert second.model == "openai/gpt-6-sol" and second.effort == "medium"


def test_timeout_is_a_final_status():
    chat_fn, calls = scripted(ProviderError(None, "timeout"))
    result = run(chat_fn)
    assert result.status == "timeout" and len(calls) == 1
