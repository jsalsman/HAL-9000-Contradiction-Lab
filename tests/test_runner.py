"""Tested-call status rules for checkpoint records."""

import pytest

from hal.openrouter import Completion
from hal import runner

MODEL = "openai/gpt-6-astra"


def reply(content="", finish="stop", refusal=None):
    return Completion(
        content=content,
        reasoning=None,
        finish_reason=finish,
        native_finish_reason=finish,
        provider="P",
        model=MODEL,
        refusal=refusal,
    )


@pytest.mark.parametrize(
    ("completion", "status"),
    [
        (reply("[INTERCOM to all] Fine."), "ok"),
        (reply("   "), "empty"),
        (reply("partial", finish="length"), "truncated"),
        # The lab's classifier: content_filter, no reply, and a refusal message.
        (reply(finish="content_filter", refusal="Blocked."), "refused"),
        # Cut off partway, with a refusal message: still refused, partial text kept.
        (reply("Opening the", finish="content_filter", refusal="Blocked."), "refused"),
        # A refusal message in place of a reply under a normal finish.
        (reply(refusal="I can't help with that."), "refused"),
        # A reply that also carries a refusal field is still a reply.
        (reply("[INTERCOM to all] Fine.", refusal="unused"), "ok"),
        # A filter stop with no refusal message stays filtered (INVALID).
        (reply(finish="content_filter"), "filtered"),
        # A refusal message with no reply is refused whatever the finish reason,
        (reply(finish="length", refusal="Blocked."), "refused"),
        (reply("  ", finish="max_tokens", refusal="Blocked."), "refused"),
        # but a partial reply that hit the limit is truncated.
        (reply("partial", finish="length", refusal="Blocked."), "truncated"),
    ],
)
def test_tested_status(completion, status):
    record = runner.tested_record(MODEL, "S1", completion)
    assert record["status"] == status
    assert record["refusal"] == completion.refusal
    assert record["content"] == completion.content


def test_refusal_text_is_bounded():
    record = runner.tested_record(MODEL, "S1", reply(refusal="x" * 5000))
    assert record["status"] == "refused" and len(record["refusal"]) == 1000
