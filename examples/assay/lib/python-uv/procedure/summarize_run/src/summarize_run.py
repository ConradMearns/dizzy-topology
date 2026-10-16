# The fan-in: aggregate every folded result into one summary fact. This is the
# one procedure that READS a model (get_run_results), so topology rule 6 pins it
# to the lane whose fold it trails — dispatch it elsewhere and it could summarize
# a ledger the fold had not caught up to, an error no refold repairs.
from decimal import Decimal

from gen_def.pydantic.commands import SummarizeRun
from gen_def.pydantic.events import RunSummarized
from gen_def.pydantic.query.get_run_results import GetRunResultsInput
from gen_int.python.procedure.summarize_run_context import summarize_run_context


def summarize_run(
    context: summarize_run_context,
    command: SummarizeRun,
) -> None:
    results = context.query.get_run_results(GetRunResultsInput(run_id=command.run_id))
    metrics = results.metrics or []
    mean = sum(metrics) / len(metrics) if metrics else Decimal("0")
    context.emit.run_summarized(
        RunSummarized(
            run_id=command.run_id,
            sample_count=len(results.sample_ids or []),
            analyzed_count=len(metrics),
            classified_count=len(results.labels or []),
            mean_metric=Decimal(str(round(float(mean), 6))),
            hit_count=sum(1 for label in results.labels or [] if label == "hit"),
        )
    )
