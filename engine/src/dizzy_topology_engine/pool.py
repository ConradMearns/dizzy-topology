"""The child side of a pool lane: run one procedure, return what it emitted.

A pool child is a PURE function of its command. It holds no store, appends nothing,
dispatches nothing, and never sees the read model: the build only lets a procedure into a
pool that reads nothing the log folds (topology rule 3) — it declares no queries, or
queries only READ-ONLY models, reference data that never changes, which the child opens
its own read-only copy of. The parent — the one writer — appends what the child returns.
So a child that dies mid-procedure has written nothing anywhere.

The child cannot be handed the host's ``Domain``: its procedures close over the host's
resources and do not pickle. The host instead names a module-level factory
(``pool_domains``) that the child calls once per worker process to build its own tables.
"""

from __future__ import annotations

import json
import multiprocessing
import multiprocessing.connection
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from dizzy_kit import Attempt, Call
from dizzy_kit.domain import Domain, Procedure
from pydantic import BaseModel


@dataclass(frozen=True)
class Job:
    """One claimed attempt, as it crosses to a child: plain data only."""

    domain: str
    command_id: str
    command_type: str
    payload: str
    """Canonical JSON, exactly as the store holds it."""
    attempt_id: str
    attempt_no: int


@dataclass(frozen=True)
class Outcome:
    """What one attempt produced, as the parent's ``_unit`` would have computed it."""

    status: str
    """``done`` | ``declined`` | ``failed``."""
    reason: str | None
    error: str | None
    emitted: list[tuple[str, dict[str, Any]]]
    """``(event name, payload dict)`` in emit order; empty unless ``done``."""
    procedure_ms: float
    # Telemetry, on the worker's wall clock (``time.time_ns``, comparable with the host's
    # on one machine). Zero when no worker ran it (a pool that broke).
    started_ns: int = 0
    finished_ns: int = 0
    pid: int = 0
    worker_start: tuple[int, int] | None = None
    """This worker's start, ``(process started, tables built)`` — on its first job only."""


_TABLES: dict[str, tuple[Procedure, type[BaseModel], dict[str, type[BaseModel]]]] = {}
_STARTED: list[tuple[int, int]] = []  # this worker's start, until its first job reports it


def _process_start_ns() -> int:
    """When this process started, on the wall clock: from /proc on Linux (to a clock tick,
    ~10 ms), otherwise when this module was imported (after interpreter startup)."""
    imported = time.time_ns()
    try:
        stat = open("/proc/self/stat").read()  # noqa: SIM115 — read once, at import
        started_ticks = int(stat.rsplit(")", 1)[1].split()[19])  # field 22: starttime
        uptime = float(open("/proc/uptime").read().split()[0])  # noqa: SIM115
        age = uptime - started_ticks / os.sysconf("SC_CLK_TCK")
        return imported - int(max(age, 0.0) * 1e9)
    except (OSError, ValueError, IndexError):
        return imported


_PROCESS_START = _process_start_ns()


def _exit_with_parent() -> None:
    """Die when the parent does. A host that crashes (or is killed) never shuts its pool
    down, and an idle spawned worker otherwise blocks on its call queue forever —
    orphaned, holding whatever the host's stdio was. Its in-flight work is no loss: the
    attempt is still ``running`` in the store and re-runs when the host reopens."""
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch() -> None:
        multiprocessing.connection.wait([parent.sentinel])
        os._exit(1)

    threading.Thread(target=watch, name="dizzy-parent-watch", daemon=True).start()


def init_worker(pool_domains: Callable[[], Sequence[Domain]]) -> None:
    """Pool initializer: tie this worker's life to the parent's, then build its tables
    once."""
    _exit_with_parent()
    for domain in pool_domains():
        for proc in domain.procedures:
            _TABLES[proc.command] = (proc, domain.commands[proc.command], dict(domain.events))
    _STARTED[:] = [(_PROCESS_START, time.time_ns())]


def run_job(job: Job) -> Outcome:
    """Run *job*'s procedure exactly as the Simple Engine's ``_unit`` does, minus the
    append — that is the parent's."""
    started_ns = time.time_ns()
    started = time.perf_counter()
    status, reason, error, emitted = "done", None, None, []
    try:
        if job.command_type not in _TABLES:
            raise LookupError(f"pool_domains built no procedure for command {job.command_type!r}")
        proc, command_cls, events = _TABLES[job.command_type]
        attempt = Attempt(
            job.command_id, job.attempt_id, job.attempt_no, job.domain, job.command_type
        )
        call = Call(events, proc.emits, attempt=attempt)
        result = proc.run(command_cls.model_validate(json.loads(job.payload)), call)
        if result is not None:
            status = "declined"
            detail = getattr(result, "detail", None)
            reason = getattr(result, "reason", None)
            reason = f"{reason}: {detail}" if detail else reason
        else:
            emitted = [
                (name, event.model_dump(mode="json", exclude_none=True)) for name, event in call.out
            ]
    except Exception as exc:
        status, error, emitted = "failed", f"{type(exc).__name__}: {exc}", []
    elapsed_ns = int((time.perf_counter() - started) * 1e9)  # one figure for both fields
    worker_start = _STARTED.pop() if _STARTED else None
    return Outcome(
        status,
        reason,
        error,
        emitted,
        elapsed_ns / 1e6,
        started_ns,
        started_ns + elapsed_ns,
        os.getpid(),
        worker_start,
    )
