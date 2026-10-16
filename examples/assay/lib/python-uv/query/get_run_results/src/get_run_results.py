# Every folded result for a run, as parallel lists ordered by sample_id.
# Defensive about stragglers: summarize_run is only dispatched once both ledgers
# are complete, but a rebuilt or partial ledger still gets a well-formed answer.
from decimal import Decimal

from gen_def.pydantic.query.get_run_results import (
    GetRunResultsInput,
    GetRunResultsOutput,
)
from gen_def.sqla.models.ledger import SampleClassification, SampleMetric
from gen_int.python.query.get_run_results import get_run_results_context


def get_run_results(
    input: GetRunResultsInput, context: get_run_results_context
) -> GetRunResultsOutput:
    session = context.adapter.session
    metrics = {
        row.sample_id: row.metric
        for row in session.query(SampleMetric).filter(SampleMetric.run_id == input.run_id)
    }
    verdicts = {
        row.sample_id: row
        for row in session.query(SampleClassification).filter(
            SampleClassification.run_id == input.run_id
        )
    }
    sample_ids = sorted(set(metrics) | set(verdicts))
    return GetRunResultsOutput(
        sample_ids=sample_ids,
        metrics=[metrics.get(sample_id, Decimal("0")) for sample_id in sample_ids],
        labels=[
            verdicts[sample_id].label if sample_id in verdicts else "unknown"
            for sample_id in sample_ids
        ],
        scores=[
            verdicts[sample_id].score if sample_id in verdicts else Decimal("0")
            for sample_id in sample_ids
        ],
    )
