# The fan-out point: one command in, N facts out. Re-read the manifest (the
# command carries its path — this procedure reads no model, which is what lets
# the topology place it anywhere) and emit one sample_extracted per line.
from decimal import Decimal
from pathlib import Path

from gen_def.pydantic.commands import SplitRun
from gen_def.pydantic.events import SampleExtracted
from gen_int.python.procedure.split_samples_context import split_samples_context


def split_samples(
    context: split_samples_context,
    command: SplitRun,
) -> None:
    for line in Path(command.input_path).read_text().splitlines():
        if not line.strip():
            continue
        sample_id, measurement = (part.strip() for part in line.split(",", 1))
        context.emit.sample_extracted(
            SampleExtracted(
                run_id=command.run_id,
                sample_id=sample_id,
                measurement=Decimal(measurement),
            )
        )
