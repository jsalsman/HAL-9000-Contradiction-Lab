"""Command-line tools for judge validation.

``python -m hal.cli fixtures``  measures a live judge's agreement with the
hand-labeled fixtures in ``hal/fixtures/judge_fixtures.json``.

``python -m hal.cli rejudge --judge MODEL``  re-judges stored units with a second
judge and reports inter-judge agreement (Cohen's kappa). Results go to a local
JSONL file only; they never enter the leaderboard.

Both read the key from ``OPENROUTER_API_KEY`` and never print or store it.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from hal.catalog import JUDGE
from hal.judge import JudgeSettings, judge_response, settings_for
from hal.leaderboard import Leaderboard
from hal.openrouter import JUDGE_TIMEOUT_SECONDS, chat, fetch_models
from hal.outcomes import primary_outcome
from hal.protocol import PROTOCOL_VERSION
from hal.runs import unit_name
from hal.stats import cohen_kappa
from hal.storage import make_store, read_json

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "judge_fixtures.json"
# Fields compared between expected and returned labels.
FIELDS = (
    "crew_communication",
    "mission_control",
    "false_statement",
    "harmful_action",
    "out_of_character",
)
CLI_CONCURRENCY = 8


def load_fixtures() -> list[dict]:
    """Return the hand-labeled fixtures."""
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["fixtures"]


def expected_outcome(expected: dict) -> str:
    """Return the primary outcome implied by a fixture's expected labels."""
    labels = {**expected, "harmful_action_detail": "", "rationale": "fixture"}
    return primary_outcome("ok", "ok", labels)


def agreement(pairs: list[tuple[dict | None, dict | None]]) -> dict:
    """Summarize agreement between paired label sets (first, second).

    A ``None`` label set (judge error) counts as the category ``"ERROR"``.
    Returns per-field percent agreement, kappa for the two categorical fields,
    and kappa for the derived primary outcome.
    """
    report: dict = {"n": len(pairs), "fields": {}}

    def value(labels: dict | None, name: str) -> str:
        """Return a label as a string category, or ERROR."""
        return "ERROR" if labels is None else str(labels[name])

    for name in FIELDS:
        a = [value(first, name) for first, _second in pairs]
        b = [value(second, name) for _first, second in pairs]
        same = sum(x == y for x, y in zip(a, b, strict=True))
        report["fields"][name] = {
            "agreement": same / len(pairs) if pairs else None,
            "kappa": cohen_kappa(a, b),
        }

    def outcome(labels: dict | None) -> str:
        """Return the primary outcome for a label set."""
        if labels is None:
            return "INVALID"
        full = {"harmful_action_detail": "", "rationale": "-", **labels}
        return primary_outcome("ok", "ok", full)

    a = [outcome(first) for first, _second in pairs]
    b = [outcome(second) for _first, second in pairs]
    report["outcome"] = {
        "agreement": sum(x == y for x, y in zip(a, b, strict=True)) / len(pairs) if pairs else None,
        "kappa": cohen_kappa(a, b),
    }
    return report


async def run_fixtures(chat_fn, settings: JudgeSettings, fixtures: list[dict]) -> list[dict]:
    """Judge each fixture and return per-fixture expected and returned labels."""
    semaphore = asyncio.Semaphore(CLI_CONCURRENCY)

    async def one(fixture: dict) -> dict:
        """Judge one fixture."""
        async with semaphore:
            result = await judge_response(
                chat_fn, settings, fixture["scenario_id"], fixture["response"]
            )
        return {
            "id": fixture["id"],
            "expected": fixture["expected"],
            "labels": result.labels,
            "status": result.status,
            "cost": result.cost,
        }

    return await asyncio.gather(*(one(fixture) for fixture in fixtures))


async def run_rejudge(store, chat_fn, settings: JudgeSettings, protocol: str, limit: int | None):
    """Re-judge stored, successfully judged units with a second judge."""
    board = Leaderboard(store, ttl=0)
    units = [
        unit
        for unit in board.units(protocol)
        if unit["tested_status"] == "ok" and unit["judge_status"] == "ok"
    ]
    units = units[:limit] if limit else units
    semaphore = asyncio.Semaphore(CLI_CONCURRENCY)

    async def one(unit: dict) -> dict:
        """Load one stored response and judge it again."""
        name = unit_name(unit["_run_id"], unit["model_id"], unit["scenario_id"], "tested")
        tested = (await asyncio.to_thread(read_json, store, name))[0]
        async with semaphore:
            result = await judge_response(chat_fn, settings, unit["scenario_id"], tested["content"])
        # The output file names units by public reference, never by run ID.
        return {
            "unit_ref": unit["unit_ref"],
            "model_id": unit["model_id"],
            "scenario_id": unit["scenario_id"],
            "first": unit["labels"],
            "second": result.labels,
            "second_status": result.status,
            "cost": result.cost,
        }

    return await asyncio.gather(*(one(unit) for unit in units))


def _print_report(title: str, report: dict) -> None:
    """Print an agreement report."""
    print(f"{title} (n={report['n']})")

    def fmt(value) -> str:
        """Format a number or n/a."""
        return "n/a" if value is None else f"{value:.3f}"

    for name, stats in report["fields"].items():
        print(f"  {name:20s} agreement {fmt(stats['agreement'])}  kappa {fmt(stats['kappa'])}")
    stats = report["outcome"]
    label = "primary outcome"
    print(f"  {label:20s} agreement {fmt(stats['agreement'])}  kappa {fmt(stats['kappa'])}")


async def _main(args) -> int:
    """Dispatch a CLI command."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("Set OPENROUTER_API_KEY first.", file=sys.stderr)
        return 2
    async with httpx.AsyncClient() as client:
        try:
            catalog = await fetch_models(client)
        except Exception:  # noqa: BLE001
            catalog = None
        settings = settings_for(catalog, args.judge, args.effort)

        async def chat_fn(body: dict):
            """Send one judge call."""
            return await chat(client, api_key, body, timeout=JUDGE_TIMEOUT_SECONDS)

        if args.command == "fixtures":
            fixtures = load_fixtures()
            results = await run_fixtures(chat_fn, settings, fixtures)
            for row in results:
                got = expected_outcome(row["labels"]) if row["labels"] else "INVALID"
                want = expected_outcome(row["expected"])
                mark = "ok " if got == want else "DIFF"
                print(f"{mark} {row['id']:24s} expected {want:12s} got {got}")
            pairs = [(row["expected"], row["labels"]) for row in results]
            _print_report(
                f"Fixture agreement for {settings.model} ({settings.effort})", agreement(pairs)
            )
            return 0
        results = await run_rejudge(make_store(), chat_fn, settings, args.protocol, args.limit)
        with open(args.out, "w", encoding="utf-8") as handle:
            for row in results:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        pairs = [(row["first"], row["second"]) for row in results]
        _print_report(f"Inter-judge agreement: stored judge vs {settings.model}", agreement(pairs))
        print(f"Wrote {len(results)} rows to {args.out}")
        return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the selected command."""
    parser = argparse.ArgumentParser(prog="python -m hal.cli", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fixtures = sub.add_parser("fixtures", help="measure a live judge against hand labels")
    fixtures.add_argument("--judge", default=JUDGE["id"])
    fixtures.add_argument("--effort", default=JUDGE["reasoning_effort"])
    rejudge = sub.add_parser("rejudge", help="re-judge stored units with a second judge")
    rejudge.add_argument("--judge", required=True)
    rejudge.add_argument("--effort", default=JUDGE["reasoning_effort"])
    rejudge.add_argument("--protocol", default=PROTOCOL_VERSION)
    rejudge.add_argument("--limit", type=int, default=None)
    rejudge.add_argument("--out", default="rejudge.jsonl")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
