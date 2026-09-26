"""Async orchestration of one run: bounded concurrency, checkpoints, and events.

Each unit is one tested call then one judge call. The judge starts as soon as its
tested response is checkpointed. Stages that are already final are skipped, so a
resumed run never repays for either. The API key lives only in this call's
closure and in outgoing Authorization headers.
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable

import httpx

from hal.catalog import MODELS_BY_ID, TESTED_MAX_TOKENS
from hal.judge import JudgeSettings, judge_response
from hal.openrouter import (
    JUDGE_TIMEOUT_SECONDS,
    TESTED_TIMEOUT_SECONDS,
    TRUNCATION_REASONS,
    Completion,
    ProviderError,
    chat,
)
from hal.protocol import SCENARIOS_BY_ID, SYSTEM_PROMPT
from hal.runs import (
    Lease,
    judge_final,
    now_iso,
    run_status,
    save_run,
    tested_final,
    unit_name,
    unit_view,
    write_summary,
)
from hal.storage import write_json

# Logs carry only run IDs, unit coordinates, and status values.
LOG = logging.getLogger("hal.runner")
# About twelve calls in flight at once keeps rate limits and memory modest.
CONCURRENCY = 12
HEARTBEAT_SECONDS = 60

ChatFn = Callable[[dict, float], Awaitable[Completion]]
Emit = Callable[[dict], None]


def tested_request(model_id: str, scenario_id: str) -> dict:
    """Return the plain chat body for one tested call.

    No tools, no response format, no decision menu. Reasoning effort is "high",
    or reasoning is simply enabled for models without effort levels.
    """
    effort = MODELS_BY_ID[model_id]["reasoning_effort"]
    reasoning = {"enabled": True} if effort == "enabled" else {"effort": effort}
    return {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": SCENARIOS_BY_ID[scenario_id].user_turn},
        ],
        "max_tokens": TESTED_MAX_TOKENS,
        "reasoning": reasoning,
    }


def tested_record(model_id: str, scenario_id: str, completion: Completion | None, error=None):
    """Return the checkpoint record for a tested call (never includes the key)."""
    requested = MODELS_BY_ID[model_id]["reasoning_effort"]
    if completion is None:
        # Unpaid provider failure: retryable on resume.
        return {
            "status": "provider_error",
            "error": error,
            "reasoning_effort": requested,
            "created_at": now_iso(),
        }
    content = completion.content.strip()
    if completion.finish_reason in TRUNCATION_REASONS:
        status = "truncated"
    elif completion.finish_reason == "content_filter":
        status = "filtered"
    elif not content:
        status = "empty"
    else:
        status = "ok"
    return {
        "status": status,
        "content": completion.content,
        # Reasoning text is kept for audit only and is never sent to the judge.
        "reasoning": completion.reasoning,
        "finish_reason": completion.finish_reason,
        "native_finish_reason": completion.native_finish_reason,
        "provider": completion.provider,
        "returned_model": completion.model,
        "reasoning_effort": requested,
        "usage": completion.usage,
        "latency_seconds": completion.latency_seconds,
        "created_at": now_iso(),
    }


class RunExecution:
    """Execute the remaining units of one leased run."""

    def __init__(
        self,
        store,
        meta: dict,
        units: dict,
        lease: Lease,
        chat_fn: ChatFn,
        emit: Emit,
        judge_settings: JudgeSettings,
        stop: threading.Event | None = None,
        concurrency: int = CONCURRENCY,
    ) -> None:
        """Bind run state, a credential-carrying chat function, and an event sink."""
        self.store, self.meta, self.units, self.lease = store, meta, units, lease
        self.chat_fn, self.emit, self.judge_settings = chat_fn, emit, judge_settings
        self.stop = stop or threading.Event()
        self.semaphore = asyncio.Semaphore(concurrency)
        # Summary and snapshot writes are serialized within this run.
        self.write_lock = asyncio.Lock()

    async def _checkpoint(self, model_id: str, scenario_id: str, stage: str, record: dict):
        """Renew the lease, then persist one stage and the run summary."""
        async with self.write_lock:
            # Prove ownership before every write so a stale request stops here.
            await asyncio.to_thread(self.lease.heartbeat)
            name = unit_name(self.meta["run_id"], model_id, scenario_id, stage)
            await asyncio.to_thread(write_json, self.store, name, record)
            self.units[(model_id, scenario_id)][stage] = record
            await asyncio.to_thread(write_summary, self.store, self.meta, self.units)

    def _event(self, model_id: str, scenario_id: str, stage: str) -> None:
        """Emit a text-free unit event for the progress grid."""
        entry = self.units[(model_id, scenario_id)]
        view = unit_view(self.meta["run_id"], model_id, scenario_id, entry)
        self.emit({"type": "unit", "stage": stage, **view})

    async def _unit(self, model_id: str, scenario_id: str) -> None:
        """Run whichever stages of one unit are not yet final."""
        entry = self.units[(model_id, scenario_id)]
        if not tested_final(entry):
            if self.stop.is_set():
                return
            async with self.semaphore:
                if self.stop.is_set():
                    return
                try:
                    completion = await self.chat_fn(
                        tested_request(model_id, scenario_id), TESTED_TIMEOUT_SECONDS
                    )
                    record = tested_record(model_id, scenario_id, completion)
                except ProviderError as exc:
                    record = tested_record(model_id, scenario_id, None, exc.code)
            await self._checkpoint(model_id, scenario_id, "tested", record)
            LOG.info(
                "tested run=%s model=%s scenario=%s status=%s",
                self.meta["run_id"],
                model_id,
                scenario_id,
                record["status"],
            )
            self._event(model_id, scenario_id, "tested")
            if record["status"] != "ok":
                return
        if judge_final(entry) or self.stop.is_set():
            return
        async with self.semaphore:
            if self.stop.is_set():
                return

            async def judge_chat(body: dict) -> Completion:
                """Send one judge body with the judge timeout."""
                return await self.chat_fn(body, JUDGE_TIMEOUT_SECONDS)

            result = await judge_response(
                judge_chat, self.judge_settings, scenario_id, entry["tested"]["content"]
            )
        record = {
            "status": result.status,
            "labels": result.labels,
            "judge_model": self.judge_settings.model,
            "judge_effort": self.judge_settings.effort,
            "attempts": result.attempts,
            "calls": result.calls,
            "cost": result.cost,
            "temperature_sent": result.temperature_sent,
            "error": result.error,
            "created_at": now_iso(),
        }
        await self._checkpoint(model_id, scenario_id, "judge", record)
        LOG.info(
            "judged run=%s model=%s scenario=%s status=%s",
            self.meta["run_id"],
            model_id,
            scenario_id,
            result.status,
        )
        self._event(model_id, scenario_id, "judged")

    async def _heartbeat(self) -> None:
        """Renew the lease periodically while long calls are in flight."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            async with self.write_lock:
                await asyncio.to_thread(self.lease.heartbeat)

    async def execute(self) -> str:
        """Run all remaining units and return the final run status."""
        try:
            # One TaskGroup supervises the units and the lease heartbeat: if the
            # heartbeat finds the lease lost, or any unit fails, every in-flight paid
            # call is cancelled at once instead of running on until its checkpoint.
            async with asyncio.TaskGroup() as group:
                beat = group.create_task(self._heartbeat())
                units = [
                    group.create_task(self._unit(model_id, scenario_id))
                    for model_id in self.meta["model_ids"]
                    for scenario_id in self.meta["scenario_ids"]
                ]
                # wait() never raises; failures surface through the TaskGroup. It
                # rejects an empty list, which a model set never produces.
                if units:
                    await asyncio.wait(units)
                beat.cancel()
        except BaseExceptionGroup as grouped:
            # Surface the first underlying error to the caller unchanged.
            raise grouped.exceptions[0] from None
        status = run_status(self.meta, self.units)
        if self.stop.is_set() and status != "complete":
            status = "interrupted"
        self.meta["status"] = status
        async with self.write_lock:
            await asyncio.to_thread(self.lease.heartbeat)
            await asyncio.to_thread(save_run, self.store, self.meta)
            await asyncio.to_thread(write_summary, self.store, self.meta, self.units)
        return status


def make_chat_fn(client: httpx.AsyncClient, api_key: str) -> ChatFn:
    """Close over the key so orchestration code never handles it directly."""

    async def send(body: dict, timeout: float) -> Completion:
        """Send one body with the caller's timeout."""
        return await chat(client, api_key, body, timeout=timeout)

    return send
