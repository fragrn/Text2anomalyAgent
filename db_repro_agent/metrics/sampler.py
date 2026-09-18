"""Non-blocking background sampler driven by the ExperimentTimeline clock."""

from __future__ import annotations

import threading
from typing import Any

from .collector import MetricsCollector
from .timeline import ExperimentPhase, ExperimentTimeline


class MetricsSampler:
    def __init__(self, collector: MetricsCollector, *, interval_seconds: float = 1.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.collector = collector
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.timeline: ExperimentTimeline | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeline: ExperimentTimeline, *, phase: ExperimentPhase = ExperimentPhase.WARMUP) -> None:
        if self.running:
            raise RuntimeError("metrics sampler is already running")
        self.timeline = timeline
        timeline.mark(phase, name=f"{phase.value}_start")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="metrics-sampler", daemon=True)
        self._thread.start()

    def set_phase(self, phase: ExperimentPhase, *, name: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        if self.timeline is None:
            raise RuntimeError("metrics sampler has not started")
        self.timeline.mark(phase, name=name, metadata=metadata)

    def stop(self, *, join_timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        if self.timeline is not None:
            self.timeline.mark(ExperimentPhase.STOPPED, name="workload_stop")

    def _run(self) -> None:
        assert self.timeline is not None
        while not self._stop.is_set():
            self.collector.collect_once(self.timeline)
            self._stop.wait(self.interval_seconds)
