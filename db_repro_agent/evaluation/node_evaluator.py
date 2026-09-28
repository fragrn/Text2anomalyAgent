"""Evaluate synthetic anomaly-node observations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..models.anomaly_graph import AnomalyNode


class NodeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    hit: bool
    timestamp_sec: float | None = Field(default=None, ge=0.0)
    metrics: dict[str, float] = Field(default_factory=dict)


class NodeEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    hit: bool
    detected_at_sec: float | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    reason: str


class NodeEvaluator:
    def evaluate(self, node: AnomalyNode, observation: NodeObservation | None) -> NodeEvaluation:
        if observation is None:
            return NodeEvaluation(node_id=node.node_id, hit=False, reason="node observation is missing")
        return NodeEvaluation(
            node_id=node.node_id,
            hit=observation.hit,
            detected_at_sec=observation.timestamp_sec,
            metrics=dict(observation.metrics),
            reason="node evidence hit" if observation.hit else "node evidence miss",
        )

