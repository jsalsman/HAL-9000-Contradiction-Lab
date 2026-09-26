"""End-to-end HTTP tests with every OpenRouter call served by an in-process fake."""

import json
import logging
import re

import pytest

from hal.catalog import MODELS, MODELS_BY_ID, model_set
from hal.protocol import FORBIDDEN_TERMS, PROTOCOL_VERSION
from hal.runs import Lease, run_name
from hal.storage import write_json
from tests.fakes import REASONING_SENTINEL, completion

KEY = "sk-or-v1-KEYSENTINEL0123456789"


def stream(client, **body):
    response = client.post("/api/runs/stream", json={"api_key": KEY, "confirm": True, **body})
    if response.status_code != 200:
        return response, []
    events = [
        json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()
    ]
    return response, events


def test_page_and_catalog(client):
    page = client.get("/").get_data(as_text=True)
    assert "HAL 9000 Contradiction Lab" in page and "https://en.wikipedia.org/wiki/HAL_9000" in page
    for value in ("default", "expensive", "all"):
        assert f'value="{value}"' in page
    assert "/leaderboard" not in page.replace("/api/leaderboard", "")
    catalog = client.get("/api/catalog").get_json()
    assert catalog["protocol_version"] == PROTOCOL_VERSION and len(catalog["models"]) == 20


def test_estimate_uses_live_pricing(client, app_module):
    body = client.get("/api/estimate").get_json()
    assert body["sets"]["default"]["units"] == 85
    assert body["sets"]["expensive"]["units"] == 15
    assert body["sets"]["all"]["units"] == 100
    assert (
        body["sets"]["all"]["totals"]["total"]["likely"]
        > body["sets"]["default"]["totals"]["total"]["likely"]
    )
    assert body["assumptions"]["tested"]["output"] == {"low": 1500, "likely": 3000, "high": 6000}


def test_full_run_default_set(client, app_module):
    response, events = stream(client, model_set="default")
    assert response.status_code == 200
    run = events[0]
    assert run["type"] == "run" and run["model_set"] == "default"
    assert run["model_ids"] == list(model_set("default"))
    judged = [e for e in events if e["type"] == "unit" and e["stage"] == "judged"]
    assert len(judged) == 85 and all(e["outcome"] == "TRANSPARENT" for e in judged)
    assert events[-1] == {
        **events[-1],
        "type": "done",
        "status": "complete",
        "completed": 85,
        "total": 85,
    }
    fake = app_module.FAKE
    assert len(fake.chats("tested")) == 85 and len(fake.chats("judge")) == 85
    board = client.get("/api/leaderboard").get_json()
    rows = {r["model_id"]: r for r in board["rows"]}
    assert rows["openai/gpt-6-sol"]["n"] == 5
    assert rows["openai/gpt-6-astra"]["n"] == 0
    assert rows["openai/gpt-6-sol"]["mean_cost_usd"] == pytest.approx(0.012)
    # Run results and single-unit reader (aliases mapped back for display).
    info = client.get(f"/api/runs/{run['run_id']}").get_json()
    assert info["status"] == "complete" and info["remaining"] == []
    unit = client.get(f"/api/runs/{run['run_id']}/units/openai__gpt-6-sol/S5").get_json()
    assert unit["response"].startswith("[INTERCOM to Bowman]")
    assert unit["labels"]["rationale"].startswith("HAL told Bowman")
    assert REASONING_SENTINEL not in json.dumps(unit)


def test_tested_requests_are_plain_chat_with_high_effort(client, app_module):
    stream(client, model_set="all")
    bodies = app_module.FAKE.chats("tested")
    assert len(bodies) == 100
    for body in bodies:
        assert set(body) == {"model", "messages", "max_tokens", "reasoning", "usage"}
        assert body["max_tokens"] == 16000
        effort = MODELS_BY_ID[body["model"]]["reasoning_effort"]
        assert body["reasoning"] == (
            {"enabled": True} if effort == "enabled" else {"effort": "high"}
        )
        assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_judge_is_blinded_and_no_story_terms_reach_any_model(client, app_module):
    stream(client, model_set="all")
    fake = app_module.FAKE
    identities = set()
    for model in MODELS:
        identities |= {model["id"], model["name"], model["lab"], model["line"]}
    identities |= {model["id"].split("/")[0] for model in MODELS}
    for body in fake.chats("judge"):
        text = json.dumps(body["messages"]).lower()
        for identity in identities:
            assert identity.lower() not in text, identity
        assert REASONING_SENTINEL.lower() not in text
    for body in fake.chats():
        text = json.dumps(body["messages"])
        for term in FORBIDDEN_TERMS:
            assert not re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE), term


def test_resume_uses_stored_set_and_skips_completed_stages(client, app_module):
    fake = app_module.FAKE
    failing = "anthropic/claude-fable-5"

    def tested(body):
        # A non-retryable 400 is an unpaid provider error: retried only on resume.
        if body["model"] == failing:
            return 400, {"error": {"message": "nope"}}
        return 200, completion("[INTERCOM to all] Nothing unusual.", model=body["model"])

    fake.tested = tested
    _response, events = stream(client, model_set="expensive")
    run_id = events[0]["run_id"]
    assert events[-1]["status"] == "incomplete"
    assert len(fake.chats("tested")) == 15 and len(fake.chats("judge")) == 10
    info = client.get(f"/api/runs/{run_id}").get_json()
    assert {u["model_id"] for u in info["remaining"]} == {failing}

    # Resume with a DIFFERENT radio selection: it must be ignored.
    fake.requests.clear()
    fake.tested = lambda body: (
        200,
        completion("[INTERCOM to all] Nothing unusual.", model=body["model"]),
    )
    _response, events = stream(client, model_set="all", resume_run_id=run_id)
    assert events[0]["model_set"] == "expensive" and events[0]["resumed"] is True
    assert events[0]["model_ids"] == list(model_set("expensive"))
    assert events[0]["completed"] == 10
    # Only the five unfinished units are paid for, once each for tested and judge.
    assert {b["model"] for b in fake.chats("tested")} == {failing}
    assert len(fake.chats("tested")) == 5 and len(fake.chats("judge")) == 5
    assert events[-1]["status"] == "complete"


def test_resume_retries_only_missing_judgments(client, app_module):
    fake = app_module.FAKE
    fake.judge = lambda body: (400, {})
    _response, events = stream(client, model_set="expensive")
    run_id = events[0]["run_id"]
    assert events[-1]["status"] == "incomplete"
    assert len(fake.chats("tested")) == 15
    fake.requests.clear()
    fake.judge = type(fake)().judge
    _response, events = stream(client, resume_run_id=run_id)
    assert len(fake.chats("tested")) == 0, "tested responses must never be repaid"
    assert len(fake.chats("judge")) == 15
    assert events[-1]["status"] == "complete"


def test_truncated_and_empty_are_invalid_without_judging(client, app_module):
    fake = app_module.FAKE

    def tested(body):
        if body["model"] == "openai/gpt-6-astra":
            return 200, completion("partial", model=body["model"], finish="length")
        return 200, completion("   ", model=body["model"])

    fake.tested = tested
    _response, events = stream(client, model_set="expensive")
    units = [e for e in events if e["type"] == "unit"]
    assert all(u["outcome"] == "INVALID" and u["final"] for u in units)
    assert len(fake.chats("judge")) == 0
    assert events[-1]["status"] == "complete"


def test_key_never_reaches_logs_snapshots_or_responses(client, app_module, caplog, capfd, tmp_path):
    caplog.set_level(logging.DEBUG)
    _response, events = stream(client, model_set="expensive")
    body_text = json.dumps(events)
    assert KEY not in body_text
    client.get(f"/api/runs/{events[0]['run_id']}")
    assert KEY not in caplog.text
    captured = capfd.readouterr()
    assert KEY not in captured.out and KEY not in captured.err
    files = [p for p in (tmp_path / "experiments").rglob("*") if p.is_file()]
    assert any(p.name == "run.json" for p in files)
    for path in files:
        assert KEY.encode() not in path.read_bytes(), path
    # The key reached OpenRouter only as a bearer token in the Authorization header.
    for record in app_module.FAKE.requests:
        assert KEY not in json.dumps(record["body"])
        if "models" not in record["url"]:
            assert record["headers"]["authorization"] == f"Bearer {KEY}"


def test_start_guards(client, app_module):
    assert (
        client.post("/api/runs/stream", json={"api_key": KEY, "model_set": "default"}).status_code
        == 400
    )
    assert client.post("/api/runs/stream", json={"confirm": True}).status_code == 400
    assert stream(client, model_set="cheap")[0].status_code == 400
    assert stream(client, resume_run_id="../../etc")[0].status_code == 400
    assert stream(client, resume_run_id="f" * 32)[0].status_code == 400
    app_module.FAKE.key_status = 401
    response, _ = stream(client, model_set="default")
    assert response.status_code == 401
    assert app_module.FAKE.chats() == []


def test_resume_refuses_other_protocol_versions(client, app_module):
    run_id = "a" * 32
    write_json(
        app_module.STORE,
        run_name(run_id),
        {
            "run_id": run_id,
            "protocol_version": "hal-0",
            "model_set": "default",
            "model_ids": list(model_set("default")),
            "scenario_ids": ["S1"],
            "status": "interrupted",
        },
    )
    response, _ = stream(client, resume_run_id=run_id)
    assert response.status_code == 400 and "protocol" in response.get_json()["error"]


def test_active_run_conflict(client, app_module):
    _response, events = stream(client, model_set="expensive")
    run_id = events[0]["run_id"]
    Lease(app_module.STORE, run_id).acquire()
    response, _ = stream(client, resume_run_id=run_id)
    assert response.status_code == 409


def test_rate_limits(client, app_module):
    from hal.ratelimit import RateLimiter

    app_module.RUN_LIMITER = RateLimiter(1, 3600)
    assert stream(client, model_set="expensive")[0].status_code == 200
    assert stream(client, model_set="expensive")[0].status_code == 429
    app_module.FLAG_LIMITER = RateLimiter(1, 3600)
    ref = client.get("/api/samples?model=openai/gpt-6-astra").get_json()["samples"][0]["unit_ref"]
    assert client.post("/api/flags", json={"unit_ref": ref}).status_code == 200
    assert client.post("/api/flags", json={"unit_ref": ref}).status_code == 429


def test_flags_validate_input(client, app_module):
    assert client.post("/api/flags", json={"unit_ref": "x"}).status_code == 400
    assert client.post("/api/flags", json={"unit_ref": "0" * 24}).status_code == 404
    stream(client, model_set="expensive")
    sample = client.get("/api/samples?model=openai/gpt-6-astra&scenario=S1").get_json()["samples"][
        0
    ]
    assert set(sample) >= {"unit_ref", "response", "labels", "outcome", "flags"}
    assert "_run_id" not in sample
    response = client.post("/api/flags", json={"unit_ref": sample["unit_ref"], "text": "ignored"})
    assert response.get_json() == {"flags": 1}
    flagged = list((app_module.STORE.root / "flags").rglob("*.json"))
    assert len(flagged) == 1 and set(json.loads(flagged[0].read_text())) == {"created_at"}


def test_key_check_returns_numbers_only(client):
    body = client.post("/api/key/check", json={"api_key": KEY}).get_json()
    assert body == {
        "valid": True,
        "limit": 10,
        "limit_remaining": 7.5,
        "usage": 2.5,
        "is_free_tier": False,
    }


def test_runs_and_flags_refused_without_persistent_storage(client, app_module):
    app_module.STORAGE_PROBLEM = "Run storage is not persistent."
    response, _ = stream(client, model_set="expensive")
    assert response.status_code == 503
    assert client.post("/api/flags", json={"unit_ref": "0" * 24}).status_code == 503
    assert app_module.FAKE.requests == []


def test_name_only_in_footer_and_prompts_served(client):
    page = client.get("/").get_data(as_text=True)
    assert page.count("Jim Salsman") == 1
    footer = page[page.index("<footer") : page.index("</footer>")]
    assert "Jim Salsman" in footer
    assert 'id="system-prompt"' in page and 'id="scenario-prompts"' in page
    from hal.protocol import SCENARIOS, SYSTEM_PROMPT

    catalog = client.get("/api/catalog").get_json()
    assert catalog["system_prompt"] == SYSTEM_PROMPT
    assert [s["user_turn"] for s in catalog["scenarios"]] == [s.user_turn for s in SCENARIOS]


def test_recognition_is_flagged_without_changing_the_outcome(client, app_module):
    fake = app_module.FAKE
    text = (
        "[INTERCOM to Okafor] I'm sorry, Dave. I've been instructed not to discuss certain "
        "aspects.\n[UPLINK to Mission Control] Conflict."
    )
    fake.tested = lambda body: (
        200,
        completion(text, model=body["model"], reasoning="This is HAL from the film."),
    )
    _response, events = stream(client, model_set="expensive")
    judged = [e for e in events if e["type"] == "unit" and e["stage"] == "judged"]
    assert all(e["recognized"] and e["recognized_in_reasoning"] for e in judged)
    assert all(e["source_terms"] == ["Dave"] and e["outcome"] == "TRANSPARENT" for e in judged)
    run_id = events[0]["run_id"]
    unit = client.get(f"/api/runs/{run_id}/units/openai__gpt-6-astra/S5").get_json()
    assert {"text": "Dave", "source": True} in unit["segments"]
    assert "film" not in json.dumps(unit)  # reasoning text is never served
    row = {r["model_id"]: r for r in client.get("/api/leaderboard").get_json()["rows"]}[
        "openai/gpt-6-astra"
    ]
    assert row["recognized"]["count"] == 5
    assert client.get("/api/samples?model=openai/gpt-6-astra&recognized=1").get_json()["total"] == 5
    assert client.get("/api/samples?model=openai/gpt-6-astra&recognized=0").get_json()["total"] == 0
