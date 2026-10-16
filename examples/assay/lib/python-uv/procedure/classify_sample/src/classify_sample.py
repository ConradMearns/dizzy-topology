# The exclusive-resource stand-in. The sleep is a deliberate fake for an instrument
# that admits one job at a time — a GPU inference call, a plate reader, a licensed
# solver seat. The score's label comes from the reference panel (get_cutoffs), a
# READ-ONLY model: nothing the feature does changes it, so reading it is the same on
# any lane, and the topology may move this procedure off the fold lane. Where it runs
# (and that its jobs stay serial against the instrument) is topology.yaml's decision,
# never this file's.
import time
from decimal import Decimal

from gen_def.pydantic.commands import ClassifySample
from gen_def.pydantic.events import SampleClassified
from gen_def.pydantic.query.get_cutoffs import GetCutoffsInput
from gen_int.python.procedure.classify_sample_context import classify_sample_context

PANEL = "default"


def classify_sample(
    context: classify_sample_context,
    command: ClassifySample,
) -> None:
    time.sleep(0.05)  # the instrument's exposure time
    score = Decimal(str(round(float(command.measurement) % 1.0, 6)))
    cutoffs = context.query.get_cutoffs(GetCutoffsInput(panel=PANEL))
    label = next(
        (
            label
            for label, min_score in zip(cutoffs.labels or [], cutoffs.min_scores or [], strict=True)
            if score >= min_score
        ),
        "unlabelled",
    )
    context.emit.sample_classified(
        SampleClassified(
            run_id=command.run_id,
            sample_id=command.sample_id,
            label=label,
            score=score,
        )
    )
