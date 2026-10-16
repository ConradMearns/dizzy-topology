# Per-sample fan-out: dispatch both analyses for every extracted sample. This
# policy reads NOTHING — the event already carries the measurement, and copying
# it into both commands is the read-moves-to-the-dispatcher pattern: because the
# analysis procedures then read nothing the log folds, topology rule 3 lets them
# run on pool lanes off the fold.
from gen_def.pydantic.commands import AnalyzeSample, ClassifySample
from gen_def.pydantic.events import SampleExtracted
from gen_int.python.policy.fan_out_analysis_context import fan_out_analysis_context


def fan_out_analysis(
    event: SampleExtracted,
    context: fan_out_analysis_context,
) -> None:
    context.emit.analyze_sample(
        AnalyzeSample(
            run_id=event.run_id,
            sample_id=event.sample_id,
            measurement=event.measurement,
        )
    )
    context.emit.classify_sample(
        ClassifySample(
            run_id=event.run_id,
            sample_id=event.sample_id,
            measurement=event.measurement,
        )
    )
