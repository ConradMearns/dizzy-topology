# Record the run summary. Merge on run_id is the idempotence guard the
# finalize policies lean on: should a replayed result ever re-fire the summary,
# the second run_summarized folds onto the same row rather than duplicating it.
from gen_def.pydantic.events import RunSummarized
from gen_def.sqla.models.ledger import RunSummary
from gen_int.python.projection.summary_store_projection import summary_store_context


def summary_store(
    event: RunSummarized,
    context: summary_store_context,
) -> None:
    context.adapter.session.merge(
        RunSummary(
            run_id=event.run_id,
            sample_count=event.sample_count,
            analyzed_count=event.analyzed_count,
            classified_count=event.classified_count,
            mean_metric=event.mean_metric,
            hit_count=event.hit_count,
        )
    )
