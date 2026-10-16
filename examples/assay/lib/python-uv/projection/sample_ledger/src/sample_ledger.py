# Record each sample's computed metric. Merge on sample_id — never a counter —
# so the progress query counts rows and a replayed sample_analyzed is absorbed
# instead of double-counted.
from gen_def.pydantic.events import SampleAnalyzed
from gen_def.sqla.models.ledger import SampleMetric
from gen_int.python.projection.sample_ledger_projection import sample_ledger_context


def sample_ledger(
    event: SampleAnalyzed,
    context: sample_ledger_context,
) -> None:
    context.adapter.session.merge(
        SampleMetric(
            sample_id=event.sample_id,
            run_id=event.run_id,
            metric=event.metric,
        )
    )
