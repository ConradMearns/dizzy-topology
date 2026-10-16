# The graph fork: one parsed run becomes two commands the engine may run on
# different lanes — the sample fan-out and the overview plot. A policy emits
# commands only; both land on the queue as the next units of work.
from gen_def.pydantic.commands import RenderPlot, SplitRun
from gen_def.pydantic.events import RunParsed
from gen_int.python.policy.start_pipeline_context import start_pipeline_context


def start_pipeline(
    event: RunParsed,
    context: start_pipeline_context,
) -> None:
    context.emit.split_run(
        SplitRun(
            run_id=event.run_id,
            input_path=event.input_path,
            sample_count=event.sample_count,
        )
    )
    context.emit.render_plot(
        RenderPlot(
            run_id=event.run_id,
            plot_path=event.plot_path,
            sample_count=event.sample_count,
        )
    )
