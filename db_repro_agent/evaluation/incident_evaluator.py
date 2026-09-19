"""Deterministic evaluator for a single Direct Incident oracle."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..metrics.timeline import ExperimentPhase, ExperimentTimeline
from ..models.common import ResultStatus
from ..models.evidence import EvidenceResult
from ..graph.evidence_registry import EvidenceRuleRegistry
from .evidence_engine import EvidenceEngine


class IncidentEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    oracle_rule_id: str
    status: ResultStatus
    hit: bool
    evidence: EvidenceResult
    reason: str


class IncidentEvaluator:
    """Map a deterministic EvidenceResult to a Direct reproduction outcome."""

    def __init__(self, registry: EvidenceRuleRegistry) -> None:
        self.engine = EvidenceEngine(registry)

    def evaluate(
        self,
        *,
        incident_id: str,
        oracle_rule_id: str,
        timeline: ExperimentTimeline,
        phase: ExperimentPhase = ExperimentPhase.OBSERVING,
        baseline_phase: ExperimentPhase = ExperimentPhase.BASELINE,
    ) -> IncidentEvaluation:
        evidence = self.engine.evaluate_rule(
            oracle_rule_id, timeline, phase=phase, baseline_phase=baseline_phase
        )
        hit = evidence.hit
        return IncidentEvaluation(
            incident_id=incident_id,
            oracle_rule_id=oracle_rule_id,
            status=ResultStatus.EXPERIMENT_SUCCESS if hit else ResultStatus.EXPERIMENT_MISS,
            hit=hit,
            evidence=evidence,
            reason="direct incident oracle hit" if hit else evidence.reason,
        )
