# Parse the run manifest — one `sample_id,measurement` line per sample — into the
# fact everything downstream keys on. The sample_count recorded here is the
# completion target the finalize policies compare progress against, and the
# plot_path derived here is where render_run_plot will write.
from pathlib import Path

from gen_def.pydantic.commands import IngestRun
from gen_def.pydantic.events import RunParsed
from gen_int.python.procedure.parse_run_context import parse_run_context


def parse_run(
    context: parse_run_context,
    command: IngestRun,
) -> None:
    manifest = Path(command.input_path)
    lines = [line for line in manifest.read_text().splitlines() if line.strip()]
    context.emit.run_parsed(
        RunParsed(
            run_id=command.run_id,
            input_path=command.input_path,
            plot_path=str(manifest.with_suffix(".plot.txt")),
            sample_count=len(lines),
        )
    )
