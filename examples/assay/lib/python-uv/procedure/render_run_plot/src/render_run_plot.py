# The other branch of the fork off run_parsed: write a trivial ASCII overview
# plot. Query-free by design, so it renders on a pool lane while the samples are
# still being analyzed — the point is the concurrent branch, not the plot.
from pathlib import Path

from gen_def.pydantic.commands import RenderPlot
from gen_def.pydantic.events import PlotRendered
from gen_int.python.procedure.render_run_plot_context import render_run_plot_context


def render_run_plot(
    context: render_run_plot_context,
    command: RenderPlot,
) -> None:
    bars = [
        f"s{index:03d} {'#' * (1 + (index * 7) % 23)}"
        for index in range(1, command.sample_count + 1)
    ]
    Path(command.plot_path).write_text(
        f"run {command.run_id} — {command.sample_count} samples\n" + "\n".join(bars) + "\n"
    )
    context.emit.plot_rendered(PlotRendered(run_id=command.run_id, plot_path=command.plot_path))
