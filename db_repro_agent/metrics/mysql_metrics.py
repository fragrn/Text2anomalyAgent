"""MySQL runtime counters converted into per-second metrics."""

from __future__ import annotations

import time
from typing import Any

from ..tools.database.mysql import MySQLAdapter


class MySQLMetricsProvider:
    STATUS_NAMES = (
        "Threads_connected",
        "Threads_running",
        "Questions",
        "Com_commit",
        "Com_rollback",
        # Current waits is retained as a diagnostic gauge. Evidence uses the
        # cumulative wait counter below because a short-lived wait can be
        # missed by periodic sampling.
        "Innodb_row_lock_current_waits",
        "Innodb_row_lock_waits",
        "Innodb_row_lock_time",
        "Slow_queries",
    )

    def __init__(self, adapter: MySQLAdapter) -> None:
        self.adapter = adapter
        self._previous: dict[str, float] | None = None
        self._previous_at: float | None = None

    def collect(self) -> dict[str, float]:
        counters = self._read_status()
        now = time.monotonic()
        metrics = {
            "threads_connected": counters.get("Threads_connected", 0.0),
            "threads_running": counters.get("Threads_running", 0.0),
            "lock_waits": counters.get("Innodb_row_lock_waits", 0.0),
            "lock_waits_current": counters.get("Innodb_row_lock_current_waits", 0.0),
            "lock_wait_time_ms": counters.get("Innodb_row_lock_time", 0.0),
            "slow_queries_total": counters.get("Slow_queries", 0.0),
        }
        if self._previous is not None and self._previous_at is not None:
            elapsed = max(now - self._previous_at, 1e-9)
            metrics["qps"] = max(0.0, counters.get("Questions", 0.0) - self._previous.get("Questions", 0.0)) / elapsed
            commits = counters.get("Com_commit", 0.0) + counters.get("Com_rollback", 0.0)
            previous_commits = self._previous.get("Com_commit", 0.0) + self._previous.get("Com_rollback", 0.0)
            metrics["tps"] = max(0.0, commits - previous_commits) / elapsed
        else:
            metrics["qps"] = 0.0
            metrics["tps"] = 0.0
        self._previous = counters
        self._previous_at = now
        return metrics

    def snapshot(self) -> dict[str, float]:
        counters = self._read_status()
        return {
            "threads_connected": counters.get("Threads_connected", 0.0),
            "threads_running": counters.get("Threads_running", 0.0),
            "lock_waits": counters.get("Innodb_row_lock_waits", 0.0),
            "lock_waits_current": counters.get("Innodb_row_lock_current_waits", 0.0),
            "lock_wait_time_ms": counters.get("Innodb_row_lock_time", 0.0),
            "slow_queries_total": counters.get("Slow_queries", 0.0),
            "questions_total": counters.get("Questions", 0.0),
            "commits_total": counters.get("Com_commit", 0.0) + counters.get("Com_rollback", 0.0),
        }

    def snapshot_supporting(self) -> dict[str, Any]:
        result = self.adapter.execute_readonly(
            "SELECT * FROM performance_schema.data_lock_waits"
        )
        if not result.success:
            return {"available": False, "error": result.error_message}
        return {"available": True, "data_lock_waits": result.data}

    def _read_status(self) -> dict[str, float]:
        placeholders = ", ".join(["%s"] * len(self.STATUS_NAMES))
        result = self.adapter.execute_readonly(
            f"SHOW GLOBAL STATUS WHERE Variable_name IN ({placeholders})", self.STATUS_NAMES
        )
        if not result.success:
            raise RuntimeError(result.error_message or "MySQL metrics query failed")
        return {row["Variable_name"]: float(row["Value"]) for row in result.data}
