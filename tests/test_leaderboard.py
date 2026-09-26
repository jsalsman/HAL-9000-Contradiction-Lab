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
