# A reference panel's cutoffs, highest first. The panel is read-only reference data
# (topology.yaml's read_only_models): this query runs against whatever copy of it the
# caller was handed — the host's, or a pool worker's own read-only connection.
from gen_def.pydantic.query.get_cutoffs import GetCutoffsInput, GetCutoffsOutput
from gen_def.sqla.models.panel import Cutoff
from gen_int.python.query.get_cutoffs import get_cutoffs_context


def get_cutoffs(
    input: GetCutoffsInput,
    context: get_cutoffs_context,
) -> GetCutoffsOutput:
    rows = (
        context.adapter.session.query(Cutoff)
        .filter(Cutoff.panel == input.panel)
        .order_by(Cutoff.min_score.desc())
        .all()
    )
    return GetCutoffsOutput(
        labels=[row.label for row in rows],
        min_scores=[row.min_score for row in rows],
    )
