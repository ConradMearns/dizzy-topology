# finalize_when_complete's twin, triggered by sample_analyzed. The two analysis
# lanes race, so either kind of result can be the run's last one home; without
# this twin the summary would never fire when an analysis trails the final
# classification. Only the invocation triggered by the actual last result sees
# progress.complete, so the pair still dispatches exactly once.
from gen_def.pydantic.commands import SummarizeRun
from gen_def.pydantic.events import SampleAnalyzed
from gen_def.pydantic.query.get_run_progress import GetRunProgressInput
from gen_int.python.policy.finalize_when_analyzed_context import finalize_when_analyzed_context


def finalize_when_analyzed(
    event: SampleAnalyzed,
    context: finalize_when_analyzed_context,
) -> None:
    progress = context.query.get_run_progress(GetRunProgressInput(run_id=event.run_id))
    if progress.complete:
        context.emit.summarize_run(SummarizeRun(run_id=event.run_id))
