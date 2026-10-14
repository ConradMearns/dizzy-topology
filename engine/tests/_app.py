"""A tiny feature, hand-built as the kit's Domain tables a generated wiring would fill.

Module-level so a spawned pool worker can import it: ``pool_domains`` below is pickled by
reference. ``start(n)`` dispatches ``square(i)`` for each ``i < n`` through a policy, and a
projection sums the squares into the read model.
"""

from __future__ import annotations

import os
import time

from dizzy_kit.domain import Domain
from pydantic import BaseModel


class Start(BaseModel):
    n: int


class Square(BaseModel):
    i: int


class Slow(BaseModel):
    ms: int


class Fail(BaseModel):
    why: str


class WhoAmI(BaseModel):
    tag: str


class Started(BaseModel):
    n: int


class Squared(BaseModel):
    i: int
    value: int


class SlowDone(BaseModel):
    ms: int


class Ran(BaseModel):
    tag: str
    pid: int


def build_domain(model: dict | None = None) -> Domain:
    """The domain over *model*, a plain dict standing in for a read model."""
    model = {} if model is None else model
    domain = Domain(
        name="squares",
        commands={
            "start": Start,
            "square": Square,
            "slow": Slow,
            "fail": Fail,
            "who_am_i": WhoAmI,
        },
        events={
            "started": Started,
            "squared": Squared,
            "slow_done": SlowDone,
            "ran": Ran,
        },
    )

    def start(command, call):
        call.emitter("started")(Started(n=command.n))

    def square(command, call):
        call.emitter("squared")(Squared(i=command.i, value=command.i * command.i))

    def slow(command, call):
        time.sleep(command.ms / 1000)
        call.emitter("slow_done")(SlowDone(ms=command.ms))

    def fail(command, call):
        raise RuntimeError(command.why)

    def who_am_i(command, call):
        call.emitter("ran")(Ran(tag=command.tag, pid=os.getpid()))

    def fan_out(event, call):
        for i in range(event.n):
            call.emitter("square")(Square(i=i))

    def total(event):
        model["total"] = model.get("total", 0) + event.value
        model["count"] = model.get("count", 0) + 1

    def record_pid(event):
        model.setdefault("pids", {})[event.tag] = event.pid

    domain.procedure("start", "start", ["started"], start)
    domain.procedure("square", "square", ["squared"], square)
    domain.procedure("slow", "slow", ["slow_done"], slow)
    domain.procedure("fail", "fail", [], fail)
    domain.procedure("who_am_i", "who_am_i", ["ran"], who_am_i)
    domain.policy("fan_out", "started", ["square"], fan_out)
    domain.projection("total", "squared", total)
    domain.projection("record_pid", "ran", record_pid)
    return domain


def pool_domains() -> list[Domain]:
    """What a pool worker builds: the same tables over a read model nobody reads. Pool
    procedures are query-free (topology rule 3), so it never matters."""
    return [build_domain()]
