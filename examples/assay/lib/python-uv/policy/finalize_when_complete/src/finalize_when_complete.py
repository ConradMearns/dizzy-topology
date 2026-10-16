# The fan-in trigger: on each classification, ask the ledger whether every
# expected sample is both analyzed and classified, and dispatch summarize_run
# when it is. The main lane folds and commits before it dispatches, so exactly
# one invocation — the one triggered by the run's last result — sees the counts
# complete; a replayed result re-firing this is absorbed by summary_store's
# merge-on-run_id fold.
from gen_def.pydantic.commands import SummarizeRun
from gen_def.pydantic.events import SampleClassified
from gen_def.pydantic.query.get_run_progress import GetRunProgressInput
from gen_int.python.policy.finalize_when_complete_context import finalize_when_complete_context


def finalize_when_complete(
    event: SampleClassified,
    context: finalize_when_complete_context,
) -> None:
    progress = context.query.get_run_progress(GetRunProgressInput(run_id=event.run_id))
    if progress.complete:
        context.emit.summarize_run(SummarizeRun(run_id=event.run_id))
