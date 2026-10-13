"""Typed context construction for final Action generation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionGenerationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target_anomaly: str = Field(min_length=1)
    target_node: str = Field(min_length=1)
    database: str = Field(min_length=1)
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    indexes: dict[str, Any] = Field(default_factory=dict)
    data_scale: dict[str, Any] = Field(default_factory=dict)
    runtime_snapshot: dict[str, Any] = Field(default_factory=dict)
    workload_status: dict[str, Any] = Field(default_factory=dict)
    duration_budget_seconds: float = Field(gt=0)
    safety_constraints: dict[str, Any] = Field(default_factory=dict)
    previous_reflection: dict[str, Any] | None = None


class ContextBuilder:
    """Build a stable, serializable context without embedding generation logic."""

    def build(
        self,
        *,
        target_anomaly: str,
        target_node: str,
        database: str,
        schema: dict[str, Any] | None = None,
        indexes: dict[str, Any] | None = None,
        data_scale: dict[str, Any] | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
        workload_status: dict[str, Any] | None = None,
        duration_budget_seconds: float,
        safety_constraints: dict[str, Any] | None = None,
        previous_reflection: dict[str, Any] | None = None,
    ) -> ActionGenerationContext:
        return ActionGenerationContext(
            target_anomaly=target_anomaly,
            target_node=target_node,
            database=database,
            schema_=schema or {},
            indexes=indexes or {},
            data_scale=data_scale or {},
            runtime_snapshot=runtime_snapshot or {},
            workload_status=workload_status or {},
            duration_budget_seconds=duration_budget_seconds,
            safety_constraints=safety_constraints or {},
            previous_reflection=previous_reflection,
        )
