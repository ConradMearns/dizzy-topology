# CPU-bound analysis of one sample: iterate the logistic map from the raw
# measurement — real arithmetic, tens of milliseconds of it, deliberately NOT a
# sleep, so a process pool shows a real speedup while a single lane shows real
# serialization. Everything it needs rides in the command (no queries), which is
# what makes it pool-eligible under topology rule 3.
from decimal import Decimal

from gen_def.pydantic.commands import AnalyzeSample
from gen_def.pydantic.events import SampleAnalyzed
from gen_int.python.procedure.compute_metrics_context import compute_metrics_context

# Calibrated to a few tens of milliseconds of arithmetic per sample — long
# enough that the pool's parallelism is visible in a profile, short enough that
# the example stays quick.
ITERATIONS = 1_500_000


def compute_metrics(
    context: compute_metrics_context,
    command: AnalyzeSample,
) -> None:
    # Seed in (0, 1): the fractional part of the measurement, defaulting off the
    # map's fixed points so the orbit actually wanders.
    x = float(command.measurement) % 1.0
    if x in (0.0, 1.0):
        x = 0.5
    for _ in range(ITERATIONS):
        x = 3.9 * x * (1.0 - x)
    context.emit.sample_analyzed(
        SampleAnalyzed(
            run_id=command.run_id,
            sample_id=command.sample_id,
            metric=Decimal(str(round(x, 6))),
        )
    )
