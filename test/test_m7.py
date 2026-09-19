"""M7 Direct Reproduction tests and acceptance checks."""

from __future__ import annotations

import time

from db_repro_agent.evaluation.incident_evaluator import IncidentEvaluator
from db_repro_agent.execution.command_executor import _iso
from db_repro_agent.metrics.collector import MetricsCollector
from db_repro_agent.metrics.sampler import MetricsSampler
from db_repro_agent.metrics.timeline import ExperimentPhase
from db_repro_agent.graph.evidence_registry import EvidenceRuleRegistry
from db_repro_agent.models.action import ActionResult, SQLAction
from db_repro_agent.models.common import ResultStatus
from db_repro_agent.reproduction.direct_runner import DirectReproductionRequest, DirectRunner


class FakeProvider:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)
        self.last = 0.0

    def collect(self) -> dict[str, float]:
        try:
            self.last = next(self.values)
        except StopIteration:
            pass
        return {"qps": self.last}


class PhaseAwareProvider:
    def __init__(self, observed_value: float) -> None:
        self.phase = ExperimentPhase.WARMUP
        self.observed_value = observed_value

    def collect(self) -> dict[str, float]:
        value = 100.0 if self.phase in {ExperimentPhase.WARMUP, ExperimentPhase.BASELINE} else self.observed_value
        return {"qps": value}


class PhaseAwareCollector(MetricsCollector):
    def __init__(self, provider: PhaseAwareProvider) -> None:
        super().__init__({"synthetic": provider})
        self.provider = provider

    def collect_once(self, timeline, *, phase=None):
        self.provider.phase = phase or timeline.current_phase
        return super().collect_once(timeline, phase=phase)


class FakeDispatcher:
    def __init__(self, result: ActionResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def validate(self, action):
        self.calls.append("validate")

    def start(self, action):
        self.calls.append("start")
        return ActionResult(
            action_id=action.action_id,
            action_type=action.type,
            success=True,
            status="started",
            started_at=_iso(time.time()),
            finished_at=_iso(time.time()),
            duration_ms=0,
        )

    def execute(self, action):
        self.calls.append("execute")
        return self.result

    def cleanup(self, action):
        self.calls.append("cleanup")
        return self.result.model_copy(update={"success": True, "status": "cleaned"})


def make_runner(observed_value: float, action_result: ActionResult) -> tuple[DirectRunner, FakeDispatcher]:
    provider = PhaseAwareProvider(observed_value)
    sampler = MetricsSampler(PhaseAwareCollector(provider), interval_seconds=0.005)
    dispatcher = FakeDispatcher(action_result)
    registry = EvidenceRuleRegistry.from_yaml("config/evidence_rules.yaml")
    return DirectRunner(
        dispatcher=dispatcher,
        sampler=sampler,
        evaluator=IncidentEvaluator(registry),
    ), dispatcher


def action_result(success: bool = True) -> ActionResult:
    return ActionResult(
        action_id="sql-1",
        action_type="sql",
        success=success,
        status="succeeded" if success else "failed",
        started_at=_iso(time.time()),
        finished_at=_iso(time.time()),
        duration_ms=10,
        error_code=None if success else 1064,
        error_message=None if success else "syntax error",
    )


def request() -> DirectReproductionRequest:
    return DirectReproductionRequest(
        incident_id="direct-m7",
        action=SQLAction(
            action_id="sql-1",
            target_node="qps_drop",
            database="tpcc10_test",
            sql="SELECT 1",
            duration_sec=1,
        ),
        oracle_rule_id="qps_drop",
        baseline_seconds=0.03,
        observe_seconds=0.03,
        recovery_seconds=0.01,
    )


def test_direct_success_closed_loop_returns_experiment_success() -> None:
    runner, dispatcher = make_runner(69, action_result())

    result = runner.run(request())

    assert result.status == ResultStatus.EXPERIMENT_SUCCESS
    assert result.evaluation is not None and result.evaluation.hit is True
    assert result.state.phase.value == "success"
    assert dispatcher.calls == ["validate", "start", "execute", "cleanup"]
    assert any(sample.phase == ExperimentPhase.BASELINE for sample in result.timeline.samples)
    assert any(sample.phase == ExperimentPhase.OBSERVING for sample in result.timeline.samples)


def test_successful_action_but_oracle_miss_is_experiment_miss() -> None:
    runner, _ = make_runner(90, action_result())

    result = runner.run(request())

    assert result.status == ResultStatus.EXPERIMENT_MISS
    assert result.action_result is not None and result.action_result.success is True
    assert result.evaluation is not None and result.evaluation.hit is False
    assert result.state.phase.value == "cleaning_up"


def test_action_failure_is_system_error_not_experiment_miss() -> None:
    runner, dispatcher = make_runner(69, action_result(success=False))

    result = runner.run(request())

    assert result.status == ResultStatus.SYSTEM_ERROR
    assert result.action_result is not None and result.action_result.success is False
    assert result.evaluation is None
    assert result.state.phase.value == "cleaning_up"
    assert "cleanup" in dispatcher.calls


def test_three_repeated_manual_direct_runs_are_stable() -> None:
    statuses = []
    for index in range(3):
        runner, _ = make_runner(69, action_result())
        result = runner.run(request().model_copy(update={"incident_id": f"direct-m7-{index}"}))
        statuses.append(result.status)

    assert statuses == [ResultStatus.EXPERIMENT_SUCCESS] * 3
