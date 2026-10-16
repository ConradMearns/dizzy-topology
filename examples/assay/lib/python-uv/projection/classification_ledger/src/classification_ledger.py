# Record each sample's classifier verdict. Merge on sample_id for the same
# reason as sample_ledger: completion is judged by counting these rows, so a
# refolded fact must land on the row it already made.
from gen_def.pydantic.events import SampleClassified
from gen_def.sqla.models.ledger import SampleClassification
from gen_int.python.projection.classification_ledger_projection import (
    classification_ledger_context,
)


def classification_ledger(
    event: SampleClassified,
    context: classification_ledger_context,
) -> None:
    context.adapter.session.merge(
        SampleClassification(
            sample_id=event.sample_id,
            run_id=event.run_id,
            label=event.label,
            score=event.score,
        )
    )
