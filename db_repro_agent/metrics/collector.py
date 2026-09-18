"""Metric provider aggregation; providers are isolated from the sampler thread."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .timeline import ExperimentPhase, ExperimentTimeline


class MetricProvider(Protocol):
    def collect(self) -> dict[str, float]: ...


class MetricsCollector:
    def __init__(self, providers: dict[str, MetricProvider], logger: logging.Logger | None = None) -> None:
        self.providers = providers
        self.logger = logger or logging.getLogger("db_repro_agent.metrics")

    def collect_once(self, timeline: ExperimentTimeline, *, phase: ExperimentPhase | None = None) -> dict[str, float]:
        collected: dict[str, float] = {}
        for provider_name, provider in self.providers.items():
            try:
                values = provider.collect()
                for metric_name, value in values.items():
                    full_name = metric_name if provider_name == "" else f"{provider_name}.{metric_name}"
                    timeline.add_sample(full_name, value, phase=phase)
                    collected[full_name] = float(value)
            except Exception as exc:
                self.logger.warning("metric provider failed provider=%s error=%s", provider_name, exc)
        return collected
