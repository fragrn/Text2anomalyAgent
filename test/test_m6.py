"""M6 tests and acceptance checks for deterministic Evidence Engine."""

from __future__ import annotations

import math
from pathlib import Path

from db_repro_agent.evaluation.evidence_engine import EvidenceEngine
from db_repro_agent.graph.evidence_registry import EvidenceRuleRegistry
from db_repro_agent.metrics.timeline import ExperimentPhase, ExperimentTimeline
from db_repro_agent.models.evidence import EvidenceRule, EvidenceStatus


def timeline_with_qps(injection_value: float, *, baseline_value: float = 100.0) -> ExperimentTimeline:
    timeline = ExperimentTimeline()
    timeline.mark(ExperimentPhase.BASELINE, name="baseline_start")
    timeline.add_sample("qps", baseline_value, phase=ExperimentPhase.BASELINE)
    timeline.mark(ExperimentPhase.INJECTION, name="injection_start")
    timeline.add_sample("qps", injection_value, phase=ExperimentPhase.INJECTION)
    return timeline


def test_registry_loads_deterministic_yaml_rules() -> None:
    registry = EvidenceRuleRegistry.from_yaml(Path("config/evidence_rules.yaml"))

    qps_rule = registry.get("qps_drop")
    assert qps_rule.metric == "qps"
    assert qps_rule.threshold == 0.7
    assert registry.get("slow_query").aggregation == "count"


def test_qps_ratio_69_percent_is_hit() -> None:
    engine = EvidenceEngine(EvidenceRuleRegistry.from_yaml("config/evidence_rules.yaml"))

    result = engine.evaluate_rule("qps_drop", timeline_with_qps(69.0))

    assert result.status == EvidenceStatus.HIT
    assert result.hit is True
    assert result.comparison_value == 0.69


def test_qps_ratio_exactly_70_percent_is_miss_for_strict_lt() -> None:
    engine = EvidenceEngine(EvidenceRuleRegistry.from_yaml("config/evidence_rules.yaml"))

    result = engine.evaluate_rule("qps_drop", timeline_with_qps(70.0))

    assert result.status == EvidenceStatus.MISS
    assert result.hit is False
    assert result.comparison_value == 0.7


def test_count_delta_and_p95_rules() -> None:
    registry = EvidenceRuleRegistry(
        rules={
            "slow": EvidenceRule(
                id="slow", metric="new_slow_log_entries", aggregation="count", reference="window_start", operator="gte", threshold=1
            ),
            "lock": EvidenceRule(
                id="lock", metric="lock_waits", aggregation="delta", reference="window_start", operator="gt", threshold=0
            ),
            "latency": EvidenceRule(
                id="latency", metric="latency", aggregation="p95", reference="absolute", operator="gte", threshold=95
            ),
        }
    )
    timeline = ExperimentTimeline()
    timeline.mark(ExperimentPhase.INJECTION, name="injection_start")
    timeline.add_sample("new_slow_log_entries", 0, phase=ExperimentPhase.INJECTION)
    timeline.add_sample("new_slow_log_entries", 1, phase=ExperimentPhase.INJECTION)
    timeline.add_sample("lock_waits", 0, phase=ExperimentPhase.INJECTION)
    timeline.add_sample("lock_waits", 2, phase=ExperimentPhase.INJECTION)
    for value in [10, 20, 95, 100, 110]:
        timeline.add_sample("latency", value, phase=ExperimentPhase.INJECTION)
    engine = EvidenceEngine(registry)

    assert engine.evaluate_rule("slow", timeline).hit is True
    assert engine.evaluate_rule("lock", timeline).hit is True
    latency = engine.evaluate_rule("latency", timeline)
    assert latency.hit is True
    assert latency.aggregate_value == 110


def test_consecutive_samples_are_required() -> None:
    rule = EvidenceRule(
        id="cpu", metric="cpu", aggregation="mean", reference="absolute", operator="gte", threshold=80, min_consecutive_samples=2
    )
    registry = EvidenceRuleRegistry(rules={"cpu": rule})
    timeline = ExperimentTimeline()
    timeline.mark(ExperimentPhase.INJECTION, name="injection_start")
    for value in [90, 70, 95, 96]:
        timeline.add_sample("cpu", value, phase=ExperimentPhase.INJECTION)

    result = EvidenceEngine(registry).evaluate_rule("cpu", timeline)

    assert result.hit is True
    assert result.longest_consecutive_samples == 2


def test_missing_nan_and_zero_baseline_are_not_hits() -> None:
    registry = EvidenceRuleRegistry(
        rules={
            "qps": EvidenceRule(id="qps", metric="qps", aggregation="ratio", reference="baseline", operator="lt", threshold=0.7),
            "cpu": EvidenceRule(id="cpu", metric="cpu", aggregation="mean", reference="absolute", operator="gt", threshold=80),
        }
    )
    missing = timeline_with_qps(69)
    nan_timeline = ExperimentTimeline()
    nan_timeline.mark(ExperimentPhase.INJECTION, name="injection_start")
    nan_timeline.add_sample("cpu", math.nan, phase=ExperimentPhase.INJECTION)
    zero_baseline = timeline_with_qps(69, baseline_value=0)

    engine = EvidenceEngine(registry)
    missing_result = engine.evaluate_rule("cpu", missing)
    nan_result = engine.evaluate_rule("cpu", nan_timeline)
    zero_result = engine.evaluate_rule("qps", zero_baseline)

    assert missing_result.status == EvidenceStatus.MISSING
    assert nan_result.status == EvidenceStatus.INVALID
    assert zero_result.status == EvidenceStatus.INVALID
    assert not missing_result.hit and not nan_result.hit and not zero_result.hit


def test_same_timeline_is_deterministic() -> None:
    registry = EvidenceRuleRegistry.from_yaml("config/evidence_rules.yaml")
    timeline = timeline_with_qps(69)
    engine = EvidenceEngine(registry)

    first = engine.evaluate_rule("qps_drop", timeline)
    second = engine.evaluate_rule("qps_drop", timeline)

    assert first == second
