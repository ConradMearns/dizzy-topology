"""Profiler tests — pure functions over a hand-written capture.

The record fixture below is authored BY HAND against the bus record contract (see
``dizzy docs cli``, "dizzy profile", and the shapes ``test_loop.py`` pins), not captured
from a live engine: deterministic ``ts``/``duration_ms`` values are what let the report
be a one-line syrupy snapshot. Each line is what a ``JsonlBusSink`` would write —
``{"seq": n, "ts": iso-utc, **engine_record}`` — including the parent-side pool
procedure shape whose ``name`` is the COMMAND class name, and the ``position`` field on
event records.
"""

import textwrap
from pathlib import Path

import pytest
from dizzy.feat_loader import load_feat
from dizzy.feat_schema import FeatureDefinition
from syrupy.assertion import SnapshotAssertion

from dizzy_topology.profiler import (
    aggregate,
    bottleneck_totals,
    command_owner,
    parse_records,
    procedure_queries,
    render_profile,
)

# A downscaled assay-shaped feature: one query-free procedure that ran in BOTH inline
# and pool modes (compute_metrics), one querying procedure (summarize_run), a policy,
# and a projection — every eligibility and normalization branch in one feat.
FEAT_YAML = """\
description: Profiler fixture

models:
  results:
    description: Analysis results
    adapters: [sqla]

queries:
  get_progress:
    description: Samples analyzed so far
    model: results
    adapter: sqla

commands:
  ingest_run: Start a run
  analyze_sample: Analyze one sample
  summarize_run: Summarize a finished run

events:
  run_parsed: The run file was parsed
  sample_analyzed: One sample's metrics were computed
  run_summarized: The run summary was written

procedures:
  parse_run:
    description: Parse the input file
    command: ingest_run
    emits: [run_parsed]
  compute_metrics:
    description: CPU-bound per-sample math
    command: analyze_sample
    emits: [sample_analyzed]
  summarize_run:
    description: Fold the results into a summary
    command: summarize_run
    queries: [get_progress]
    emits: [run_summarized]

policies:
  fan_out_analysis:
    description: Dispatch one analysis per sample
    event: run_parsed
    emits: [analyze_sample]

projections:
  results_ledger:
    description: Fold sample metrics into the results model
    event: sample_analyzed
    model: results
    adapter: sqla
"""


@pytest.fixture
def profile_feat(tmp_path: Path) -> FeatureDefinition:
    feat_file = tmp_path / "profile.feat.yaml"
    feat_file.write_text(FEAT_YAML)
    return load_feat(feat_file)


def _ts(second: int) -> str:
    return f"2026-01-01T00:00:{second:02d}+00:00"


# One run's worth of records, in sink order. compute_metrics appears three ways: inline
# (feat name, lane "main"), pool ok (command class name "AnalyzeSample", lane "compute"),
# and pool error — all must aggregate under the one feat name.
RECORDS = [
    {
        "seq": 1,
        "ts": _ts(0),
        "kind": "event",
        "name": "run_parsed",
        "trigger": "abc123def456",
        "duration_ms": None,
        "outcome": "appended",
        "detail": "",
        "position": 1,
    },
    {
        "seq": 2,
        "ts": _ts(0),
        "kind": "procedure",
        "name": "parse_run",
        "trigger": "IngestRun",
        "duration_ms": 12.5,
        "outcome": "ok",
        "detail": "",
    },
    {
        "seq": 3,
        "ts": _ts(0),
        "kind": "policy",
        "name": "fan_out_analysis",
        "trigger": "run_parsed",
        "duration_ms": 3.0,
        "outcome": "ok",
        "detail": "",
    },
    {
        "seq": 4,
        "ts": _ts(1),
        "kind": "procedure",
        "name": "compute_metrics",
        "trigger": "AnalyzeSample",
        "duration_ms": 55.0,
        "outcome": "ok",
        "detail": "",
        "lane": "main",
    },
    {
        "seq": 5,
        "ts": _ts(1),
        "kind": "procedure",
        "name": "AnalyzeSample",
        "trigger": "AnalyzeSample",
        "duration_ms": 40.125,
        "outcome": "ok",
        "detail": "",
        "lane": "compute",
        "pid": 4242,
    },
    {
        "seq": 6,
        "ts": _ts(1),
        "kind": "procedure",
        "name": "AnalyzeSample",
        "trigger": "AnalyzeSample",
        "duration_ms": 60.375,
        "outcome": "ok",
        "detail": "",
        "lane": "compute",
        "pid": 4243,
    },
    {
        "seq": 7,
        "ts": _ts(2),
        "kind": "procedure",
        "name": "AnalyzeSample",
        "trigger": "AnalyzeSample",
        "duration_ms": 10.0,
        "outcome": "error",
        "detail": "ValueError: boom",
        "lane": "compute",
        "pid": 4242,
    },
    {
        "seq": 8,
        "ts": _ts(2),
        "kind": "event",
        "name": "sample_analyzed",
        "trigger": "bbb222ccc333",
        "duration_ms": None,
        "outcome": "appended",
        "detail": "",
        "position": 2,
    },
    {
        "seq": 9,
        "ts": _ts(2),
        "kind": "event",
        "name": "sample_analyzed",
        "trigger": "ddd444eee555",
        "duration_ms": None,
        "outcome": "appended",
        "detail": "",
        "position": 3,
    },
    {
        "seq": 10,
        "ts": _ts(2),
        "kind": "projection",
        "name": "results_ledger",
        "trigger": "sample_analyzed",
        "duration_ms": 5.0,
        "outcome": "ok",
        "detail": "",
    },
    {
        "seq": 11,
        "ts": _ts(2),
        "kind": "projection",
        "name": "results_ledger",
        "trigger": "sample_analyzed",
        "duration_ms": 7.0,
        "outcome": "ok",
        "detail": "",
    },
    {
        "seq": 12,
        "ts": _ts(3),
        "kind": "procedure",
        "name": "summarize_run",
        "trigger": "SummarizeRun",
        "duration_ms": 20.0,
        "outcome": "ok",
        "detail": "",
    },
    {
        "seq": 13,
        "ts": _ts(4),
        "kind": "event",
        "name": "run_summarized",
        "trigger": "fff666aaa777",
        "duration_ms": None,
        "outcome": "appended",
        "detail": "",
        "position": 4,
    },
]


def test_render_profile_snapshot(profile_feat, snapshot: SnapshotAssertion) -> None:
    assert render_profile(profile_feat, RECORDS) == snapshot


def test_parse_records_skips_unparseable_lines() -> None:
    lines = textwrap.dedent(
        """\
        {"seq": 1, "ts": "2026-01-01T00:00:00+00:00", "kind": "event", "name": "e"}
        not json at all
        [1, 2, 3]

        {"seq": 2, "ts": "2026-01-01T00:00:01+00:00", "kind": "policy", "name": "p"
        {"seq": 3, "ts": "2026-01-01T00:00:02+00:00", "kind": "policy", "name": "p"}
        """
    ).splitlines()

    records, skipped = parse_records(lines)

    # The blank line costs nothing; the garbage line, the non-object, and the
    # truncated line cost one each — telemetry loss is tolerated, never silent.
    assert skipped == 3
    assert [r["seq"] for r in records] == [1, 3]


def test_mixed_naive_and_aware_stamps_cost_the_span_not_the_profile(profile_feat) -> None:
    # One hand-edited (or foreign-writer) stamp without an offset alongside the sink's
    # "+00:00" ones: both parse, the subtraction raises TypeError — the span line goes
    # unknown, the rest of the report still renders.
    records = [
        {**RECORDS[1], "ts": "2026-01-01T00:00:00"},  # naive
        RECORDS[3],  # aware, later — min/max pick one of each
    ]

    report = render_profile(profile_feat, records)

    assert "Capture span: unknown" in report
    assert "compute_metrics" in report


def test_command_owner_maps_pascal_command_to_procedure(profile_feat) -> None:
    assert command_owner(profile_feat) == {
        "IngestRun": "parse_run",
        "AnalyzeSample": "compute_metrics",
        "SummarizeRun": "summarize_run",
    }


def test_aggregate_normalizes_pool_records_onto_the_feat_name(profile_feat) -> None:
    profile = aggregate(profile_feat, RECORDS)

    # 1 inline + 2 pool ok + 1 pool error = one element, four invocations.
    stats = profile.elements[("procedure", "compute_metrics")]
    assert ("procedure", "AnalyzeSample") not in profile.elements
    assert stats.count == 4
    assert stats.errors == 1
    assert stats.lanes == {"main", "compute"}
    assert stats.total_ms == pytest.approx(165.5)


def test_aggregate_counts_fan_out(profile_feat) -> None:
    profile = aggregate(profile_feat, RECORDS)

    assert profile.event_counts == {
        "run_parsed": 1,
        "sample_analyzed": 2,
        "run_summarized": 1,
    }
    assert profile.policy_counts == {"fan_out_analysis": 1}


def test_pool_eligibility_is_read_from_the_feat(profile_feat) -> None:
    queries = procedure_queries(profile_feat)

    assert queries["parse_run"] == []
    assert queries["compute_metrics"] == []
    assert queries["summarize_run"] == ["get_progress"]


def test_bottleneck_totals_split_on_declared_queries(profile_feat) -> None:
    profile = aggregate(profile_feat, RECORDS)

    serial, eligible = bottleneck_totals(profile_feat, profile)

    # Serial-bound: policy 3.0 + projections 12.0 + querying summarize_run 20.0.
    # Pool-eligible: query-free parse_run 12.5 + compute_metrics 165.5.
    assert serial == pytest.approx(35.0)
    assert eligible == pytest.approx(178.0)


def test_query_timings_are_reported_but_not_double_counted(profile_feat) -> None:
    records = [
        {
            "ts": _ts(0),
            "kind": "procedure",
            "name": "summarize_run",
            "duration_ms": 20.0,
            "outcome": "ok",
        },
        {
            "ts": _ts(1),
            "kind": "query",
            "name": "get_progress",
            "duration_ms": 4.0,
            "outcome": "ok",
            "input_items": 1,
            "result_items": 20,
            "input_shape": "one",
            "result_shape": "small",
        },
        {
            "ts": _ts(2),
            "kind": "query",
            "name": "get_progress",
            "duration_ms": 8.0,
            "outcome": "error",
            "input_items": 1,
            "input_shape": "one",
        },
    ]
    profile = aggregate(profile_feat, records)

    stats = profile.elements[("query", "get_progress")]
    assert stats.count == 2
    assert stats.errors == 1
    assert stats.total_ms == pytest.approx(12.0)
    assert stats.input_items == 2
    assert stats.result_items == 20
    assert stats.access_patterns == {"one->small": 1}
    assert bottleneck_totals(profile_feat, profile) == pytest.approx((20.0, 0.0))
    report = render_profile(profile_feat, records)
    assert "query" in report
    assert "get_progress" in report
    assert "p50" in report
    assert "input items" in report
    assert "one->small:1" in report


def test_bottleneck_counts_undeclared_procedures_as_serial(profile_feat) -> None:
    # A capture naming a procedure the feat does not declare (stale capture, wrong
    # feat): eligibility is a promise read from the feat, so the stranger gets none.
    records = [
        {
            "seq": 1,
            "ts": _ts(0),
            "kind": "procedure",
            "name": "who_is_this",
            "trigger": "WhoIsThis",
            "duration_ms": 9.0,
            "outcome": "ok",
            "detail": "",
        },
    ]
    profile = aggregate(profile_feat, records)

    serial, eligible = bottleneck_totals(profile_feat, profile)

    assert serial == pytest.approx(9.0)
    assert eligible == 0.0


def test_read_only_models_make_their_readers_pool_eligible(profile_feat) -> None:
    """With the topology's read-only models passed in, a procedure that queries only
    those is pool-eligible, and the report says which reads it is exempt for."""
    assert procedure_queries(profile_feat, ["results"])["summarize_run"] == []
    profile = aggregate(profile_feat, RECORDS)
    serial, eligible = bottleneck_totals(profile_feat, profile, ["results"])
    assert serial == pytest.approx(15.0)
    assert eligible == pytest.approx(198.0)
    report = render_profile(profile_feat, RECORDS, ["results"])
    assert "yes (read-only: get_progress)" in report
    assert "readers of read-only models" in report
