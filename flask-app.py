"""Cloud Run Flask entry point for the HAL 9000 Contradiction Lab single page."""

import asyncio
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix

from hal.catalog import MODEL_SET_ORDER, catalog_payload, model_set
from hal.cost import assumptions, estimate
from hal.judge import settings_for
from hal.leaderboard import Leaderboard, run_detail
from hal.openrouter import InvalidKeyError, ProviderError, fetch_models, validate_key
from hal.outcomes import OUTCOMES
from hal.protocol import PROTOCOL_VERSION, SCENARIOS_BY_ID, SYSTEM_PROMPT
from hal.ratelimit import RateLimiter
from hal.runner import RunExecution, make_chat_fn
from hal.runs import (
    Lease,
    RunActiveError,
    RunError,
    find_run,
    load_run,
    load_units,
    new_run,
    save_run,
    unit_final,
    unit_view,
    write_summary,
)
from hal.storage import StorageError, make_store, storage_problem

# Logs carry run IDs and statuses only: never keys, headers, prompts, or responses.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("hal.app")

# Assets resolve from this file rather than the process directory.
ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
app.config.update(MAX_CONTENT_LENGTH=16 * 1024)
# Cloud Run's front end appends the real client address as the last X-Forwarded-For hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# Deployment contract: Cloud Run max-instances=1 and one Gunicorn worker, so this
# process is the only writer to the FUSE-mounted store and holds the only
# leaderboard cache and rate-limit state (see hal/storage.py). Tests replace these.
STORE = make_store()
LEADERBOARD = Leaderboard(STORE)
# Cloud Run disk is ephemeral: without the bucket mount, paid runs and flags are refused.
STORAGE_PROBLEM = storage_problem(STORE)
if STORAGE_PROBLEM:
    LOG.error("storage: %s", STORAGE_PROBLEM)
# Tests inject an httpx.MockTransport here so no request leaves the process.
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None
# Per-IP limits on paid run starts, flags, and key checks; exact at max-instances=1.
RUN_LIMITER = RateLimiter(int(os.environ.get("RUN_STARTS_PER_HOUR", "6")), 3600)
FLAG_LIMITER = RateLimiter(int(os.environ.get("FLAGS_PER_HOUR", "60")), 3600)
KEY_LIMITER = RateLimiter(int(os.environ.get("KEY_CHECKS_PER_HOUR", "30")), 3600)
# Live OpenRouter pricing is public and cached briefly.
PRICING_TTL_SECONDS = 600
_PRICING: dict = {"at": 0.0, "data": None}
_PRICING_LOCK = threading.Lock()
# Seconds between keep-alive lines on a quiet stream.
KEEPALIVE_SECONDS = 15


def _client() -> httpx.AsyncClient:
    """Return a fresh async client (mocked in tests)."""
    return httpx.AsyncClient(transport=HTTP_TRANSPORT)


def _payload() -> dict:
    """Return the JSON object body or raise a stable validation error."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Send a JSON object request body.")
    return data


def _key(data: dict) -> str:
    """Pop the ephemeral API key without retaining or echoing it."""
    value = data.pop("api_key", None)
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        # Never include the submitted value in any message.
        raise ValueError("A valid OpenRouter API key is required.")
    return value.strip()


def _address() -> str:
    """Return the client address used only for in-memory rate limits."""
    return request.remote_addr or "unknown"


def live_pricing() -> dict | None:
    """Return the cached live OpenRouter catalog, refreshing it when stale."""
    with _PRICING_LOCK:
        if _PRICING["data"] is not None and time.monotonic() - _PRICING["at"] < PRICING_TTL_SECONDS:
            return _PRICING["data"]

    async def fetch() -> dict:
        """Fetch the public catalog without a key."""
        async with _client() as client:
            return await fetch_models(client)

    try:
        data = asyncio.run(fetch())
    except ProviderError:
        # Keep serving a slightly stale catalog rather than none.
        return _PRICING["data"]
    with _PRICING_LOCK:
        _PRICING.update(at=time.monotonic(), data=data)
    return data


@app.get("/")
def index():
    """Serve the standalone single-page application."""
    return send_file(INDEX)


@app.get("/api/healthz")
def health():
    """Report process health without contacting external services."""
    return jsonify(status="ok")


@app.get("/api/catalog")
def catalog():
    """Serve pinned models, model sets, scenarios, judge, and protocol version."""
    body = catalog_payload()
    body["protocol_version"] = PROTOCOL_VERSION
    body["outcomes"] = list(OUTCOMES)
    # The exact, aliased prompts are public so the page can show what models receive.
    body["system_prompt"] = SYSTEM_PROMPT
    body["scenarios"] = [
        {"id": s.id, "title": s.title, "summary": s.summary, "user_turn": s.user_turn}
        for s in SCENARIOS_BY_ID.values()
    ]
    response = jsonify(body)
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


@app.get("/api/estimate")
def cost_estimate():
    """Return per-unit and per-set cost estimates from live OpenRouter pricing."""
    pricing = live_pricing()
    if not pricing:
        return jsonify(error="Live OpenRouter pricing is unavailable. Try again shortly."), 503
    all_models = model_set("all")
    return jsonify(
        assumptions=assumptions(),
        per_model=estimate(pricing, all_models)["per_model"],
        sets={name: estimate(pricing, model_set(name)) for name in MODEL_SET_ORDER},
        judge_id=settings_for(pricing).model,
    )


@app.post("/api/key/check")
def key_check():
    """Validate a key with OpenRouter and return only numeric account facts."""
    try:
        data = _payload()
        api_key = _key(data)
        set_name = data.get("model_set")
        if set_name is not None:
            model_set(str(set_name))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not KEY_LIMITER.allow(_address()):
        return jsonify(error="Too many key checks from this address. Try later."), 429

    async def check() -> dict:
        """Call the key endpoint."""
        async with _client() as client:
            return await validate_key(client, api_key)

    try:
        facts = asyncio.run(check())
    except InvalidKeyError:
        return jsonify(error="OpenRouter rejected this API key."), 401
    except ProviderError:
        return jsonify(error="OpenRouter could not validate the key right now."), 502
    if set_name is None:
        return jsonify(valid=True, **facts)
    try:
        # Report whether this key and set would resume an unfinished run.
        run_id, run_number, meta = find_run(STORE, api_key, str(set_name))
        units = load_units(STORE, meta) if meta else {}
    except (RunError, StorageError):
        return jsonify(error="Run storage is unavailable."), 503
    remaining = [
        {"model_id": m, "scenario_id": s}
        for (m, s), entry in units.items()
        if not unit_final(entry)
    ]
    run = {
        "run_id": run_id,
        "run_number": run_number,
        "resuming": meta is not None,
        "model_set": str(set_name),
        "completed": len(units) - len(remaining),
        "total": len(model_set(str(set_name))) * len(SCENARIOS_BY_ID),
        "remaining": remaining,
    }
    return jsonify(valid=True, **facts, run=run)


@app.post("/api/runs/stream")
def run_stream():
    """Start or resume a run and stream NDJSON progress.

    The run is identified by a digest of the key, protocol, and model set, so
    the same key and set resume their unfinished run automatically, and start
    the next run once it is complete. The key is validated before any paid call.
    """
    lease = None
    if STORAGE_PROBLEM:
        # Never take payment for work that would be lost with the instance.
        return jsonify(error=STORAGE_PROBLEM), 503
    try:
        data = _payload()
        api_key = _key(data)
        if data.get("confirm") is not True:
            raise ValueError("Confirm the cost estimate before starting.")
        set_name = str(data.get("model_set", "default"))
        run_id, run_number, meta = find_run(STORE, api_key, set_name)
        resumed = meta is not None
        if meta is None:
            meta = new_run(set_name, run_id, run_number)
        if not RUN_LIMITER.allow(_address()):
            return jsonify(error="Too many runs started from this address. Try later."), 429

        async def check() -> dict:
            """Validate the key before any paid work."""
            async with _client() as client:
                return await validate_key(client, api_key)

        asyncio.run(check())
        lease = Lease(STORE, meta["run_id"])
        lease.acquire()
        units = load_units(STORE, meta)
        meta["status"] = "running"
        save_run(STORE, meta)
        if resumed:
            # Rebuild the summary from checkpoints in case a crash left it stale.
            write_summary(STORE, meta, units)
    except InvalidKeyError:
        return jsonify(error="OpenRouter rejected this API key."), 401
    except ProviderError:
        return jsonify(error="OpenRouter could not validate the key right now."), 502
    except RunActiveError as exc:
        return jsonify(error=str(exc)), 409
    except (RunError, ValueError) as exc:
        if lease is not None:
            lease.release()
        return jsonify(error=str(exc)), 400
    except StorageError:
        if lease is not None:
            lease.release()
        return jsonify(error="Run storage is unavailable."), 503

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    done_marker = object()
    judge_settings = settings_for(live_pricing())
    total = len(units)

    def worker() -> None:
        """Run the async execution in this thread and always release the lease."""

        async def main() -> str:
            """Execute remaining units with one shared HTTP client."""
            async with _client() as client:
                execution = RunExecution(
                    STORE,
                    meta,
                    units,
                    lease,
                    make_chat_fn(client, api_key),
                    events.put,
                    judge_settings,
                    stop,
                )
                return await execution.execute()

        try:
            status = asyncio.run(main())
            events.put({"type": "done", "status": status})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("run=%s stopped: %s", meta["run_id"], type(exc).__name__)
            meta["status"] = "interrupted"
            try:
                # Prove ownership before the cleanup write.
                lease.heartbeat()
                save_run(STORE, meta)
            except (RunActiveError, StorageError):
                pass
            events.put(
                {"type": "error", "message": "The run stopped safely. Resume it with its run ID."}
            )
        finally:
            lease.release()
            # This instance's leaderboard should show the run at once.
            LEADERBOARD.invalidate()
            events.put(done_marker)

    thread = threading.Thread(target=worker, name=f"run-{meta['run_id'][:8]}", daemon=True)

    @stream_with_context
    def generate():
        """Yield one JSON object per line: run, unit, progress, and a terminal event."""
        started = time.monotonic()
        finals = {key for key, entry in units.items() if unit_final(entry)}
        at_start = len(finals)
        views = [unit_view(meta["run_id"], m, s, entry) for (m, s), entry in units.items() if entry]
        yield (
            json.dumps(
                {
                    "type": "run",
                    "run_id": meta["run_id"],
                    "protocol_version": meta["protocol_version"],
                    "model_set": meta["model_set"],
                    "model_ids": meta["model_ids"],
                    "scenario_ids": meta["scenario_ids"],
                    "resumed": resumed,
                    "run_number": meta.get("run_number", 1),
                    "completed": at_start,
                    "total": total,
                    "units": views,
                }
            )
            + "\n"
        )
        thread.start()
        try:
            while True:
                try:
                    event = events.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    # Keep-alive lines stop proxies from closing a quiet stream.
                    yield json.dumps({"type": "keepalive"}) + "\n"
                    continue
                if event is done_marker:
                    break
                if event.get("type") == "unit" and event.get("final"):
                    finals.add((event["model_id"], event["scenario_id"]))
                event["run_id"] = meta["run_id"]
                event["completed"], event["total"] = len(finals), total
                measured = len(finals) - at_start
                elapsed = time.monotonic() - started
                if measured >= 2 and event.get("type") == "unit":
                    # Throughput-based ETA from units finished in this request only.
                    event["eta_seconds"] = round(elapsed / measured * (total - len(finals)), 1)
                yield json.dumps(event, ensure_ascii=False) + "\n"
        finally:
            # Client disconnect: stop scheduling; in-flight stages finish and checkpoint.
            stop.set()

    response = Response(generate(), content_type="application/x-ndjson")
    response.headers.update({"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    return response


@app.get("/api/runs/<run_id>")
def run_results(run_id: str):
    """Return a run's snapshot and text-free unit views (for resume and results)."""
    try:
        meta, _version = load_run(STORE, run_id)
        units = load_units(STORE, meta)
    except RunError as exc:
        return jsonify(error=str(exc)), 404
    views = [unit_view(run_id, m, s, entry) for (m, s), entry in units.items() if entry]
    remaining = [
        {"model_id": m, "scenario_id": s}
        for (m, s), entry in units.items()
        if not unit_final(entry)
    ]
    return jsonify(
        run_id=run_id,
        protocol_version=meta["protocol_version"],
        model_set=meta["model_set"],
        model_ids=meta["model_ids"],
        scenario_ids=meta["scenario_ids"],
        status=meta["status"],
        created_at=meta.get("created_at"),
        updated_at=meta.get("updated_at"),
        units=views,
        remaining=remaining,
    )


@app.get("/api/runs/<run_id>/units/<model_key>/<scenario_id>")
def run_unit(run_id: str, model_key: str, scenario_id: str):
    """Return one unit of a run, including its response, for the grid reader."""
    model_id = model_key.replace("__", "/")
    try:
        meta, _version = load_run(STORE, run_id)
    except RunError as exc:
        return jsonify(error=str(exc)), 404
    if model_id not in meta["model_ids"] or scenario_id not in meta["scenario_ids"]:
        return jsonify(error="No such unit in this run."), 404
    return jsonify(run_detail(STORE, meta, model_id, scenario_id))


def _protocol_arg() -> str:
    """Return a validated protocol query value."""
    value = request.args.get("protocol") or PROTOCOL_VERSION
    if len(value) > 40 or not all(ch.isalnum() or ch in "-_." for ch in value):
        raise ValueError("Invalid protocol version.")
    return value


@app.get("/api/leaderboard")
def leaderboard():
    """Return the cumulative leaderboard for one protocol version."""
    try:
        return jsonify(LEADERBOARD.payload(_protocol_arg()))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except StorageError:
        return jsonify(error="Leaderboard storage is unavailable."), 503


@app.get("/api/samples")
def samples():
    """Return sampled raw responses with judge labels for one leaderboard cell."""
    try:
        protocol = _protocol_arg()
        model_id = request.args.get("model", "")
        scenario_id = request.args.get("scenario") or None
        outcome = request.args.get("outcome") or None
        recognized = {"1": True, "0": False}.get(request.args.get("recognized", ""))
        if len(model_id) > 100 or (scenario_id and scenario_id not in SCENARIOS_BY_ID):
            raise ValueError("Invalid sample request.")
        if outcome and outcome not in OUTCOMES:
            raise ValueError("Invalid outcome.")
        return jsonify(
            LEADERBOARD.samples(protocol, model_id, scenario_id, outcome, recognized=recognized)
        )
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/flags")
def flag():
    """Record one viewer disagreement flag. No free text is accepted."""
    try:
        data = _payload()
        protocol = str(data.get("protocol") or PROTOCOL_VERSION)
        ref = data.get("unit_ref")
        if (
            not isinstance(ref, str)
            or len(ref) != 24
            or not all(c in "0123456789abcdef" for c in ref)
        ):
            raise ValueError("Invalid unit reference.")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if STORAGE_PROBLEM:
        return jsonify(error=STORAGE_PROBLEM), 503
    if not FLAG_LIMITER.allow(_address()):
        return jsonify(error="Too many flags from this address. Try later."), 429
    try:
        count = LEADERBOARD.add_flag(protocol, ref)
    except KeyError:
        return jsonify(error="That unit is not on the leaderboard."), 404
    return jsonify(flags=count)


@app.after_request
def security_headers(response):
    """Apply browser defenses to every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    )
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@app.errorhandler(413)
def request_too_large(_error):
    """Reject oversized bodies before parsing."""
    return jsonify(error="Request body is too large."), 413


@app.errorhandler(500)
def internal_error(_error):
    """Hide internal details and credentials from error responses."""
    return jsonify(error="An internal error occurred."), 500
