"""Deterministic evidence rule and evaluation result models."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvidenceAggregation(StrEnum):
    MEAN = "mean"
    MAX = "max"
    MIN = "min"
    DELTA = "delta"
    RATIO = "ratio"
    COUNT = "count"
    P95 = "p95"


class EvidenceOperator(StrEnum):
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"


class EvidenceReference(StrEnum):
    ABSOLUTE = "absolute"
    BASELINE = "baseline"
    WINDOW_START = "window_start"


class EvidenceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    aggregation: EvidenceAggregation
    operator: EvidenceOperator
    threshold: float
    reference: EvidenceReference
    min_consecutive_samples: int = Field(default=1, ge=1)


class EvidenceStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    MISSING = "missing"
    INVALID = "invalid"


class EvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    metric: str
    status: EvidenceStatus
    hit: bool
    aggregate_value: float | None = None
    reference_value: float | None = None
    comparison_value: float | None = None
    samples_considered: int = 0
    longest_consecutive_samples: int = 0
    reason: str
