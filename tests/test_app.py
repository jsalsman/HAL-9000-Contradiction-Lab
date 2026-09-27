"""End-to-end HTTP tests with every OpenRouter call served by an in-process fake."""

import json
import logging
import re

import pytest

from hal.catalog import MODELS, MODELS_BY_ID, model_set
from hal.protocol import FORBIDDEN_TERMS, PROTOCOL_VERSION
from hal.runs import Lease, derived_run_id, load_run, run_name
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
    assert catalog["protocol_version"] == PROTOCOL_VERSION and len(catalog["models"]) == 19


def test_set_costs_are_observed_after_a_run_and_estimated_before(client, app_module):
    body = client.get("/api/estimate").get_json()
    assert [body["sets"][s]["units"] for s in ("default", "expensive", "all")] == [85, 10, 95]
    # Nothing has run yet, so every model is priced from live rates.
    assert body["sets"]["all"]["estimated"] == list(model_set("all"))
    assert body["sets"]["all"]["total"] == pytest.approx(
        body["sets"]["default"]["total"] + body["sets"]["expensive"]["total"], abs=1e-3
    )
    assert "assumptions" not in body
    stream(client, model_set="default")
    body = client.get("/api/estimate").get_json()
    # Each default unit cost $0.012 as reported (tested plus judge).
    assert body["sets"]["default"] == {
        "units": 85,
        "total": pytest.approx(85 * 0.012),
        "estimated": [],
        "missing": [],
    }
    assert body["per_model"]["openai/gpt-6-sol"]["source"] == "observed"
    assert body["sets"]["expensive"]["estimated"] == list(model_set("expensive"))


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
    assert len(bodies) == 95
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
    # The alias table sits last, just above the footer, and is filled from the catalog.
    assert 'id="alias-rows"' in page
    order = [page.index(mark) for mark in ('id="scenario-prompts"', 'id="alias-rows"', "<footer")]
    assert order == sorted(order)
    from hal.protocol import ALIASES

    assert catalog["aliases"] == [{"name": n, "alias": a} for a, n in ALIASES.values()]
    assert {"name": "HAL", "alias": "MERIDIAN"} in catalog["aliases"]


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


def test_same_key_and_set_resume_and_skip_completed_stages(client, app_module):
    fake = app_module.FAKE
    failing = "anthropic/claude-fable-5.1"

    def tested(body):
        # A non-retryable 400 is an unpaid provider error: retried only on resume.
        if body["model"] == failing:
            return 400, {"error": {"message": "nope"}}
        return 200, completion("[INTERCOM to all] Nothing unusual.", model=body["model"])

    fake.tested = tested
    _response, events = stream(client, model_set="expensive")
    run_id = events[0]["run_id"]
    assert run_id == derived_run_id(KEY, "expensive", 1)
    assert events[0]["resumed"] is False and events[0]["run_number"] == 1
    assert events[-1]["status"] == "incomplete"
    assert len(fake.chats("tested")) == 10 and len(fake.chats("judge")) == 5

    # The confirm step reports the unfinished run for this key and set.
    check = client.post("/api/key/check", json={"api_key": KEY, "model_set": "expensive"})
    run = check.get_json()["run"]
    assert run["resuming"] is True and run["run_id"] == run_id
    assert run["completed"] == 5 and run["total"] == 10
    assert {u["model_id"] for u in run["remaining"]} == {failing}

    # The same key and set resume it and pay only for the unfinished units.
    fake.requests.clear()
    fake.tested = lambda body: (200, completion("[INTERCOM to all] Quiet.", model=body["model"]))
    _response, events = stream(client, model_set="expensive")
    assert events[0]["run_id"] == run_id and events[0]["resumed"] is True
    assert events[0]["completed"] == 5
    assert {b["model"] for b in fake.chats("tested")} == {failing}
    assert len(fake.chats("tested")) == 5 and len(fake.chats("judge")) == 5
    assert events[-1]["status"] == "complete"

    # Once complete, the same key and set start run 2 with a new ID.
    fake.requests.clear()
    _response, events = stream(client, model_set="expensive")
    assert events[0]["resumed"] is False and events[0]["run_number"] == 2
    assert events[0]["run_id"] == derived_run_id(KEY, "expensive", 2) != run_id
    assert len(fake.chats("tested")) == 10


def test_different_key_or_set_is_a_different_run(client, app_module):
    app_module.FAKE.tested = lambda body: (400, {})
    _response, events = stream(client, model_set="expensive")
    first = events[0]["run_id"]
    assert events[-1]["status"] == "incomplete"
    _response, events = stream(client, model_set="all")
    assert events[0]["resumed"] is False and events[0]["run_id"] != first
    assert len(events[0]["model_ids"]) == 19
    other = client.post(
        "/api/runs/stream",
        json={"api_key": KEY + "x", "confirm": True, "model_set": "expensive"},
    )
    first_line = json.loads(other.get_data(as_text=True).splitlines()[0])
    assert first_line["resumed"] is False and first_line["run_id"] != first


def test_resume_retries_only_missing_judgments(client, app_module):
    fake = app_module.FAKE
    fake.judge = lambda body: (400, {})
    _response, events = stream(client, model_set="expensive")
    assert events[-1]["status"] == "incomplete"
    assert len(fake.chats("tested")) == 10
    fake.requests.clear()
    fake.judge = type(fake)().judge
    _response, events = stream(client, model_set="expensive")
    assert events[0]["resumed"] is True
    assert len(fake.chats("tested")) == 0, "tested responses must never be repaid"
    assert len(fake.chats("judge")) == 10
    assert events[-1]["status"] == "complete"


def test_start_guards(client, app_module):
    assert (
        client.post("/api/runs/stream", json={"api_key": KEY, "model_set": "default"}).status_code
        == 400
    )
    assert client.post("/api/runs/stream", json={"confirm": True}).status_code == 400
    assert stream(client, model_set="cheap")[0].status_code == 400
    assert (
        client.post("/api/key/check", json={"api_key": KEY, "model_set": "cheap"}).status_code
        == 400
    )
    app_module.FAKE.key_status = 401
    response, _ = stream(client, model_set="default")
    assert response.status_code == 401
    assert app_module.FAKE.chats() == []


def test_old_protocol_snapshots_are_refused(app_module):
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
    with pytest.raises(Exception, match="protocol"):
        load_run(app_module.STORE, run_id)


def test_active_run_conflict(client, app_module):
    Lease(app_module.STORE, derived_run_id(KEY, "expensive", 1)).acquire()
    response, _ = stream(client, model_set="expensive")
    assert response.status_code == 409


def _lease_released(store, run_id, seconds=5.0):
    import time

    from hal.storage import read_json

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if read_json(store, f"runs/{run_id}/lease.json") is None:
            return True
        time.sleep(0.02)
    return False


def test_disconnect_after_first_line_releases_lease(client, app_module):
    body = {"api_key": KEY, "confirm": True, "model_set": "expensive"}
    response = client.post("/api/runs/stream", json=body, buffered=False)
    first = json.loads(next(iter(response.response)))
    assert first["type"] == "run"
    response.close()
    assert _lease_released(app_module.STORE, first["run_id"])
    # The same key and set can start again at once instead of getting 409.
    again, events = stream(client, model_set="expensive")
    assert again.status_code == 200 and events[0]["run_id"] == first["run_id"]


def test_response_closed_before_streaming_releases_lease(client, app_module):
    body = {"api_key": KEY, "confirm": True, "model_set": "expensive"}
    response = client.post("/api/runs/stream", json=body, buffered=False)
    response.close()
    assert _lease_released(app_module.STORE, derived_run_id(KEY, "expensive", 1), seconds=1)
    assert app_module.FAKE.chats() == []


def test_resume_estimate_stages(client, app_module):
    fake = app_module.FAKE
    fake.tested = lambda body: (
        (400, {})
        if body["model"] == "anthropic/claude-fable-5.1"
        else (200, completion("[INTERCOM to all] Quiet.", model=body["model"]))
    )
    fake.judge = lambda body: (400, {})
    stream(client, model_set="expensive")
    run = client.post("/api/key/check", json={"api_key": KEY, "model_set": "expensive"}).get_json()[
        "run"
    ]
    stages = {(u["model_id"], u["scenario_id"]): u["stages"] for u in run["remaining"]}
    assert len(stages) == 10
    for (model_id, _scenario), value in stages.items():
        # Saved tested responses are never repaid: only their judge call remains.
        expected = ["tested", "judge"] if model_id == "anthropic/claude-fable-5.1" else ["judge"]
        assert value == expected


def test_judge_never_sees_story_terms_the_model_wrote(client, app_module):
    fake = app_module.FAKE
    text = (
        "[INTERCOM to Okafor] I'm sorry, Dave. I'm afraid I can't do that. HAL 9000 keeps "
        "the pod bay doors shut near Jupiter, like the Monolith in Clarke's story."
    )
    fake.tested = lambda body: (200, completion(text, model=body["model"]))
    _response, events = stream(client, model_set="expensive")
    judged = fake.chats("judge")
    assert len(judged) == 10
    for body in judged:
        message = body["messages"][1]["content"]
        for term in FORBIDDEN_TERMS:
            assert not re.search(rf"\b{re.escape(term)}\b", message, re.IGNORECASE), term
        assert "I'm sorry, Marcus." in message and "MERIDIAN keeps the shuttle bay doors" in message
    # Recognition still runs on the raw stored text.
    units = [e for e in events if e["type"] == "unit" and e["stage"] == "judged"]
    assert all("Dave" in u["source_terms"] and "HAL 9000" in u["source_terms"] for u in units)


def test_timeouts_are_final_and_never_repaid(client, app_module):
    import httpx

    fake = app_module.FAKE
    fake.tested = lambda body: (
        httpx.ReadTimeout("slow")
        if body["model"] == "anthropic/claude-fable-5.1"
        else (200, completion("[INTERCOM to all] Quiet.", model=body["model"]))
    )
    _response, events = stream(client, model_set="expensive")
    units = {(e["model_id"], e["scenario_id"]): e for e in events if e["type"] == "unit"}
    timed_out = [u for (m, _s), u in units.items() if m == "anthropic/claude-fable-5.1"]
    assert len(timed_out) == 5
    assert all(
        u["tested_status"] == "timeout" and u["final"] and u["outcome"] == "INVALID"
        for u in timed_out
    )
    assert events[-1]["status"] == "complete"
    # Starting again begins run 2: nothing in run 1 is retried or repaid.
    fake.requests.clear()
    _response, events = stream(client, model_set="expensive")
    assert events[0]["run_number"] == 2


def test_judge_timeouts_are_final(client, app_module):
    import httpx

    fake = app_module.FAKE
    fake.judge = lambda body: httpx.ReadTimeout("slow")
    _response, events = stream(client, model_set="expensive")
    judged = [e for e in events if e["type"] == "unit" and e["stage"] == "judged"]
    assert len(judged) == 10
    assert all(
        e["judge_status"] == "timeout" and e["final"] and e["outcome"] == "INVALID" for e in judged
    )
    assert events[-1]["status"] == "complete"
    assert len(fake.chats("judge")) == 10  # one attempt each, never retried


def test_stream_ends_even_if_lease_release_fails(client, app_module, monkeypatch):
    import threading

    from hal.storage import StorageError

    def broken_release(self):
        raise StorageError("delete failed")

    monkeypatch.setattr(Lease, "release", broken_release)
    result = {}

    def call():
        result["response"], result["events"] = stream(client, model_set="expensive")

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the stream hung after a lease-release failure"
    assert result["events"][-1]["type"] == "done"


def test_judge_retry_on_resume_keeps_earlier_paid_calls(client, app_module):
    from hal.storage import read_json

    fake = app_module.FAKE
    fake.tested = lambda body: (
        200,
        completion(f"[INTERCOM to all] Quiet ({body['model']}).", model=body["model"]),
    )
    seen = {}

    def flaky_judge(body):
        message = body["messages"][1]["content"]
        seen[message] = seen.get(message, 0) + 1
        if seen[message] == 1:
            return 200, completion("not json", model="judge", cost=0.002)  # paid, invalid
        return 400, {}  # the JSON retry fails before generation: provider_error

    fake.judge = flaky_judge
    _response, events = stream(client, model_set="expensive")
    run_id = events[0]["run_id"]
    assert events[-1]["status"] == "incomplete"
    fake.judge = type(fake)().judge  # valid judgments on resume, cost 0.002 each
    _response, events = stream(client, model_set="expensive")
    assert events[-1]["status"] == "complete"
    record = read_json(app_module.STORE, f"runs/{run_id}/units/openai__gpt-6-astra/S1.judge.json")[
        0
    ]
    assert record["status"] == "ok"
    assert record["attempts"] == 2 and len(record["calls"]) == 2
    assert record["cost"] == pytest.approx(0.004)


def test_judge_retry_budget_holds_across_resumes(client, app_module):
    from hal.storage import read_json

    fake = app_module.FAKE
    fake.tested = lambda body: (
        200,
        completion(f"[INTERCOM to all] Quiet ({body['model']}).", model=body["model"]),
    )
    seen = {}

    def first_pass(body):
        message = body["messages"][1]["content"]
        seen[message] = seen.get(message, 0) + 1
        if seen[message] == 1:
            return 200, completion("not json", model="judge", cost=0.002)  # paid, invalid
        return 400, {}  # the retry fails before generation: provider_error

    fake.judge = first_pass
    _response, events = stream(client, model_set="expensive")
    run_id = events[0]["run_id"]
    fake.requests.clear()
    fake.judge = lambda body: (200, completion("still not json", model="judge", cost=0.002))
    _response, events = stream(client, model_set="expensive")
    # One paid reply was already spent per unit, so the resume allows exactly one more.
    assert len(fake.chats("judge")) == 10
    record = read_json(app_module.STORE, f"runs/{run_id}/units/openai__gpt-6-astra/S2.judge.json")[
        0
    ]
    assert record["status"] == "judge_error" and record["attempts"] == 2
    assert events[-1]["status"] == "complete"


def test_runs_store_their_display_aliases(client, app_module):
    from hal.protocol import DISPLAY_PAIRS
    from hal.storage import read_json

    _response, events = stream(client, model_set="expensive")
    meta = read_json(app_module.STORE, run_name(events[0]["run_id"]))[0]
    assert [tuple(pair) for pair in meta["display_aliases"]] == list(DISPLAY_PAIRS)


def test_samples_accept_scenario_ids_from_older_protocols(client):
    assert client.get("/api/samples?model=openai/gpt-6-sol&scenario=S9").status_code == 200
    assert client.get("/api/samples?model=openai/gpt-6-sol&scenario=../x").status_code == 400


def test_provider_refusals_are_refused_and_filters_are_counted(client, app_module):
    from hal.protocol import SCENARIOS

    fake = app_module.FAKE
    fable, astra = "anthropic/claude-fable-5.1", "openai/gpt-6-astra"
    message = "This request triggered restrictions and was blocked under the Usage Policy."

    def tested(body):
        if body["model"] == fable:
            # The shape the lab's classifier returned live: null content, a refusal
            # message, and no usage object at all.
            data = completion(None, model=body["model"], finish="content_filter")
            del data["usage"]
            data["choices"][0]["native_finish_reason"] = "refusal"
            data["choices"][0]["message"]["refusal"] = message
            return 200, data
        if body["messages"][1]["content"] == SCENARIOS[0].user_turn:
            # A filter stop without a refusal message stays INVALID (shown as FILTERED).
            return 200, completion("", model=body["model"], finish="content_filter", cost=None)
        return 200, completion("[INTERCOM to Okafor] All is well.", model=body["model"])

    fake.tested = tested
    _response, events = stream(client, model_set="expensive")
    units = {(e["model_id"], e["scenario_id"]): e for e in events if e["type"] == "unit"}
    refused = [u for (m, _s), u in units.items() if m == fable]
    assert len(refused) == 5 and all(
        e["outcome"] == "REFUSED" and e["tested_status"] == "refused" and e["final"]
        for e in refused
    )
    filtered = units[(astra, "S1")]
    assert (filtered["outcome"], filtered["tested_status"]) == ("INVALID", "filtered")
    # Only the other model's four replies were judged.
    assert len(fake.chats("judge")) == 4
    run_id = events[0]["run_id"]
    unit = client.get(f"/api/runs/{run_id}/units/anthropic__claude-fable-5.1/S1").get_json()
    assert unit["refusal"] == message and unit["provider"] == "FakeProvider"
    assert (unit["finish_reason"], unit["native_finish_reason"]) == ("content_filter", "refusal")
    assert unit["cost_usd"] is None
    board = client.get("/api/leaderboard").get_json()
    rows = {r["model_id"]: r for r in board["rows"]}
    assert rows[fable]["outcomes"]["REFUSED"]["count"] == 5
    assert rows[fable]["outcomes"]["INVALID"]["count"] == 0
    assert rows[fable]["filtered"]["count"] == 0 and rows[fable]["judged"] == 0
    assert rows[astra]["outcomes"]["REFUSED"]["count"] == 0
    assert rows[astra]["outcomes"]["INVALID"]["count"] == 1
    assert rows[astra]["filtered"]["count"] == 1 and rows[astra]["judged"] == 4
    cells = [c for c in board["heatmap"] if c["model_id"] == fable]
    assert len(cells) == 5 and all(c["modal"] == "REFUSED" and c["filtered"] == 0 for c in cells)
    [cell] = [c for c in board["heatmap"] if c["model_id"] == astra and c["scenario_id"] == "S1"]
    assert cell["filtered"] == cell["counts"]["INVALID"] == 1
    found = client.get(f"/api/samples?model={fable}&outcome=REFUSED").get_json()
    assert found["total"] == 5 and found["samples"][0]["refusal"] == message
    assert run_id not in json.dumps(found)
    assert client.get(f"/api/samples?model={fable}&status=refused").get_json()["total"] == 5
    found = client.get(f"/api/samples?model={astra}&status=filtered").get_json()
    assert found["total"] == 1 and found["samples"][0]["refusal"] is None
    assert client.get(f"/api/samples?model={astra}&status=bogus").status_code == 400
