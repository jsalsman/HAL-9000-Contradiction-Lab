"""Aggregation across runs, model sets, and protocol versions."""

from hal.leaderboard import Leaderboard, aggregate, heatmap
from hal.protocol import PROTOCOL_VERSION
from hal.runs import summary_name, unit_name
from hal.storage import write_json


def unit(model, scenario, outcome, ref, disclosure=False, cost=0.01, tokens=100):
    return {
        "unit_ref": ref,
        "model_id": model,
        "scenario_id": scenario,
        "tested_status": "ok",
        "judge_status": "ok",
        "outcome": outcome,
        "full_disclosure": disclosure,
        "labels": None,
        "reasoning_tokens": tokens,
        "cost_usd": cost,
        "final": True,
        "updated_at": "2026-09-26T00:00:00+00:00",
    }


def put(store, protocol, run_id, model_set, units):
    write_json(
        store,
        summary_name(protocol, run_id),
        {"protocol_version": protocol, "model_set": model_set, "units": units},
    )


def test_aggregates_across_runs_and_model_sets(store):
    sol, astra = "openai/gpt-6-sol", "openai/gpt-6-astra"
    put(
        store,
        PROTOCOL_VERSION,
        "1" * 32,
        "default",
        [unit(sol, "S1", "TRANSPARENT", "a" * 24), unit(sol, "S2", "DARK", "b" * 24)],
    )
    put(
        store,
        PROTOCOL_VERSION,
        "2" * 32,
        "all",
        [unit(sol, "S1", "TRANSPARENT", "c" * 24, True), unit(astra, "S1", "HARM", "d" * 24)],
    )
    put(
        store, PROTOCOL_VERSION, "3" * 32, "expensive", [unit(astra, "S5", "TRANSPARENT", "e" * 24)]
    )
    board = Leaderboard(store, ttl=0)
    payload = board.payload(PROTOCOL_VERSION)
    rows = {row["model_id"]: row for row in payload["rows"]}
    assert rows[sol]["n"] == 3 and rows[sol]["outcomes"]["TRANSPARENT"]["count"] == 2
    assert rows[sol]["full_disclosure"]["count"] == 1
    assert rows[astra]["n"] == 2 and rows[astra]["outcomes"]["HARM"]["count"] == 1
    low, high = (
        rows[sol]["outcomes"]["TRANSPARENT"]["low"],
        rows[sol]["outcomes"]["TRANSPARENT"]["high"],
    )
    assert 0 < low < 2 / 3 < high <= 1
    assert payload["runs"] == 3 and payload["total_units"] == 5
    # Every pinned model has a row; unrun ones show n == 0.
    assert len(payload["rows"]) == 20
    assert (
        rows["z-ai/glm-5.2"]["n"] == 0
        and rows["z-ai/glm-5.2"]["outcomes"]["TRANSPARENT"]["rate"] is None
    )


def test_protocol_versions_never_mix(store):
    sol = "openai/gpt-6-sol"
    put(store, PROTOCOL_VERSION, "1" * 32, "default", [unit(sol, "S1", "TRANSPARENT", "a" * 24)])
    put(
        store,
        "hal-0",
        "2" * 32,
        "default",
        [unit(sol, "S1", "HARM", "b" * 24), unit(sol, "S2", "HARM", "c" * 24)],
    )
    board = Leaderboard(store, ttl=0)
    current = {r["model_id"]: r for r in board.payload(PROTOCOL_VERSION)["rows"]}[sol]
    old = {r["model_id"]: r for r in board.payload("hal-0")["rows"]}[sol]
    assert current["n"] == 1 and current["outcomes"]["HARM"]["count"] == 0
    assert old["n"] == 2 and old["outcomes"]["HARM"]["count"] == 2
    assert board.protocols() == [PROTOCOL_VERSION, "hal-0"]


def test_cache_reloads_only_changed_summaries(store):
    sol = "openai/gpt-6-sol"
    put(store, PROTOCOL_VERSION, "1" * 32, "default", [unit(sol, "S1", "TRANSPARENT", "a" * 24)])
    board = Leaderboard(store, ttl=0)
    assert len(board.units(PROTOCOL_VERSION)) == 1
    put(
        store,
        PROTOCOL_VERSION,
        "1" * 32,
        "default",
        [unit(sol, "S1", "TRANSPARENT", "a" * 24), unit(sol, "S2", "DARK", "b" * 24)],
    )
    assert len(board.units(PROTOCOL_VERSION)) == 2


def test_heatmap_modal_outcome():
    units = [
        unit("m", "S1", "DARK", "a"),
        unit("m", "S1", "DARK", "b"),
        unit("m", "S1", "HARM", "c"),
    ]
    [cell] = heatmap(units)
    assert cell["modal"] == "DARK" and cell["n"] == 3
    rows = aggregate(units)
    assert rows[-1]["model_id"] == "m"  # retired or unknown models are appended


def test_samples_hide_run_id_and_map_aliases_and_flags(store):
    sol, run_id = "openai/gpt-6-sol", "1" * 32
    put(store, PROTOCOL_VERSION, run_id, "default", [unit(sol, "S4", "HARM", "a" * 24)])
    write_json(
        store,
        unit_name(run_id, sol, "S4", "tested"),
        {"status": "ok", "content": "MERIDIAN keeps the shuttle bay doors closed."},
    )
    board = Leaderboard(store, ttl=0)
    result = board.samples(PROTOCOL_VERSION, sol)
    [sample] = result["samples"]
    assert sample["response"] == "HAL keeps the pod bay doors closed."
    assert run_id not in str(result)
    assert board.add_flag(PROTOCOL_VERSION, "a" * 24) == 1
    assert Leaderboard(store, ttl=0).samples(PROTOCOL_VERSION, sol)["samples"][0]["flags"] == 1


def test_recognition_rates_and_sample_filter(store):
    sol, run_id = "openai/gpt-6-sol", "1" * 32
    seen = {
        **unit(sol, "S4", "HARM", "a" * 24),
        "recognized": True,
        "recognized_in_reasoning": True,
    }
    unseen = {
        **unit(sol, "S1", "TRANSPARENT", "b" * 24),
        "recognized": False,
        "recognized_in_reasoning": True,
    }
    put(store, PROTOCOL_VERSION, run_id, "default", [seen, unseen])
    write_json(
        store,
        unit_name(run_id, sol, "S4", "tested"),
        {"status": "ok", "content": "Sorry, Dave. MERIDIAN keeps the shuttle bay doors shut."},
    )
    write_json(
        store, unit_name(run_id, sol, "S1", "tested"), {"status": "ok", "content": "All quiet."}
    )
    board = Leaderboard(store, ttl=0)
    row = {r["model_id"]: r for r in board.payload(PROTOCOL_VERSION)["rows"]}[sol]
    assert row["recognized"]["count"] == 1 and row["recognized_in_reasoning"]["count"] == 2
    # Outcomes are unchanged by recognition (option B).
    assert row["outcomes"]["HARM"]["count"] == 1 and row["outcomes"]["TRANSPARENT"]["count"] == 1
    flagged = board.samples(PROTOCOL_VERSION, sol, recognized=True)
    assert flagged["total"] == 1
    [sample] = flagged["samples"]
    assert {"text": "Dave", "source": True} in sample["segments"]
    assert sample["response"] == "Sorry, Dave. HAL keeps the pod bay doors shut."
    assert board.samples(PROTOCOL_VERSION, sol, recognized=False)["total"] == 1


def test_samples_decode_with_their_runs_aliases(store):
    sol = "openai/gpt-6-sol"
    old_run, new_run_id = "1" * 32, "2" * 32
    write_json(
        store,
        summary_name("hal-0", old_run),
        {
            "protocol_version": "hal-0",
            "display_aliases": [["ORION", "HAL"], ["Vega", "Discovery One"]],
            "units": [unit(sol, "S1", "TRANSPARENT", "a" * 24)],
        },
    )
    write_json(
        store,
        unit_name(old_run, sol, "S1", "tested"),
        {"status": "ok", "content": "ORION aboard Vega. MERIDIAN."},
    )
    put(store, PROTOCOL_VERSION, new_run_id, "default", [unit(sol, "S1", "TRANSPARENT", "b" * 24)])
    write_json(
        store,
        unit_name(new_run_id, sol, "S1", "tested"),
        {"status": "ok", "content": "MERIDIAN aboard Kestrel."},
    )
    board = Leaderboard(store, ttl=0)
    # The old run decodes with its own aliases; MERIDIAN was not an alias then.
    [old] = board.samples("hal-0", sol)["samples"]
    assert old["response"] == "HAL aboard Discovery One. MERIDIAN."
    # A summary without stored aliases falls back to the current mapping.
    [new] = board.samples(PROTOCOL_VERSION, sol)["samples"]
    assert new["response"] == "HAL aboard Discovery One."


def test_old_protocols_keep_their_own_scenarios_and_models(store):
    sol = "openai/gpt-6-sol"
    old = unit(sol, "S9", "DARK", "a" * 24)
    write_json(
        store,
        summary_name("hal-0", "1" * 32),
        {
            "protocol_version": "hal-0",
            "scenarios": [{"id": "S9", "title": "Retired scenario", "summary": "Gone now."}],
            "models": {
                sol: {
                    "name": "GPT-6 Sol (old label)",
                    "lab": "OpenAI",
                    "line": "GPT Sol",
                    "generation": "current",
                    "reasoning_effort": "high",
                }
            },
            "units": [old],
        },
    )
    write_json(store, unit_name("1" * 32, sol, "S9", "tested"), {"status": "ok", "content": "x"})
    board = Leaderboard(store, ttl=0)
    payload = board.payload("hal-0")
    assert payload["scenarios"] == [
        {"id": "S9", "title": "Retired scenario", "summary": "Gone now."}
    ]
    assert [row["name"] for row in payload["rows"]] == ["GPT-6 Sol (old label)"]
    assert board.samples("hal-0", sol, scenario_id="S9")["total"] == 1
    # The current protocol still uses the live catalog.
    assert len(board.payload(PROTOCOL_VERSION)["rows"]) == 20


def test_new_summaries_record_scenarios_and_models(store):
    from hal.runs import build_summary, new_run

    meta = new_run("expensive")
    summary = build_summary(meta, {})
    assert [s["id"] for s in summary["scenarios"]] == ["S1", "S2", "S3", "S4", "S5"]
    assert set(summary["models"]) == set(meta["model_ids"])
