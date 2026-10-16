# How far along a run is: the declared sample count vs how many results each
# ledger holds. `complete` is computed here, in one place, so both finalize
# policies apply the identical completion rule.
from gen_def.pydantic.query.get_run_progress import (
    GetRunProgressInput,
    GetRunProgressOutput,
)
from gen_def.sqla.models.ledger import AssayRun, SampleClassification, SampleMetric
from gen_int.python.query.get_run_progress import get_run_progress_context


def get_run_progress(
    input: GetRunProgressInput, context: get_run_progress_context
) -> GetRunProgressOutput:
    session = context.adapter.session
    run = session.get(AssayRun, input.run_id)
    expected = run.sample_count if run is not None else 0
    analyzed = session.query(SampleMetric).filter(SampleMetric.run_id == input.run_id).count()
    classified = (
        session.query(SampleClassification)
        .filter(SampleClassification.run_id == input.run_id)
        .count()
    )
    return GetRunProgressOutput(
        expected=expected,
        analyzed=analyzed,
        classified=classified,
        complete=expected > 0 and analyzed >= expected and classified >= expected,
    )
