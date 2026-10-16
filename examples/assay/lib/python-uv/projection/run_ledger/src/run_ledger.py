# Record each parsed run and its expected sample count — the denominator every
# progress check divides by. Merge on run_id so a replayed fact folds to the
# same row (folds must be idempotent: replay and at-least-once both refold).
from gen_def.pydantic.events import RunParsed
from gen_def.sqla.models.ledger import AssayRun
from gen_int.python.projection.run_ledger_projection import run_ledger_context


def run_ledger(
    event: RunParsed,
    context: run_ledger_context,
) -> None:
    context.adapter.session.merge(
        AssayRun(
            run_id=event.run_id,
            input_path=event.input_path,
            plot_path=event.plot_path,
            sample_count=event.sample_count,
        )
    )
