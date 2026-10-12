"""Run or plan human-authored anomaly-chain cases.

Examples:
    python -m anomalychains.run_cases --list
    python -m anomalychains.run_cases --case 12_missing_index_plan_slow_qps
    python -m anomalychains.run_cases --all
    python -m anomalychains.run_cases --all --run-real --include-risky
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from db_repro_agent.evaluation.node_evaluator import NodeObservation
from db_repro_agent.config import ExperimentProfile, load_experiment_profile
from db_repro_agent.execution.dispatcher import ActionDispatcher
from db_repro_agent.metrics.collector import MetricsCollector
from db_repro_agent.metrics.buffer_pool import summarize_buffer_pool_window
from db_repro_agent.metrics.mysql_metrics import MySQLMetricsProvider
from db_repro_agent.metrics.sampler import MetricsSampler
from db_repro_agent.metrics.system_metrics import SystemMetricsProvider
from db_repro_agent.metrics.timeline import ExperimentPhase
from db_repro_agent.models.action import BenchBaseAction, ChaosBladeAction, SQLAction, TransactionAction, TransactionActor, TransactionStep
from db_repro_agent.models.anomaly_graph import AnomalyEdge, AnomalyGraph, AnomalyNode
from db_repro_agent.reproduction.injection_actions import build_backup_action, build_missing_index_action, build_redo_log_pressure_action
from db_repro_agent.reproduction.propagation_runner import PropagationReproductionRequest, PropagationRunner
from db_repro_agent.runtime.experiment_setup import MySQLInstrumentation
from db_repro_agent.runtime.preflight import run_preflight
from db_repro_agent.tools.database.mysql import MySQLAdapter, MySQLConfig

from .cases import CASES, CASE_BY_ID, ChainCase


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "experiment_runs" / "anomalychains_human"
PROFILE_PATH = REPO_ROOT / "config" / "experiment_profile.yaml"


def _dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _config(env: dict[str, str]) -> MySQLConfig:
    return MySQLConfig(
        host=env.get("DBMAGS_MYSQL_HOST", "127.0.0.1"),
        port=int(env.get("DBMAGS_MYSQL_PORT", "3306")),
        user=env.get("DBMAGS_MYSQL_USER", "root"),
        password=env.get("DBMAGS_MYSQL_PASSWORD", ""),
        database=env.get("DBMAGS_MYSQL_DB", "tpcc10_test"),
        connect_timeout=30,
    )


def _graph(case: ChainCase) -> AnomalyGraph:
    nodes = [AnomalyNode(node_id=node) for node in case.chain]
    edges = [
        AnomalyEdge(source=source, target=target, mechanism="human_specified", provenance="human", confidence=1.0)
        for source, target in zip(case.chain, case.chain[1:])
    ]
    return AnomalyGraph(graph_id=case.case_id, nodes=nodes, edges=edges, root_nodes=[case.root_node])


def _sql_root(case: ChainCase, database: str):
    if case.root_node == "backup":
        return build_backup_action(f"{case.case_id}-root", database, backup_table=f"dbmags_backup_{case.case_id[:8]}")
    if case.root_node in {"missing_index", "excessive_index"}:
        action = build_missing_index_action(f"{case.case_id}-root", database)
        return action.model_copy(update={"target_node": case.root_node})
    if case.root_node == "redo_log_pressure":
        return build_redo_log_pressure_action(f"{case.case_id}-root", database)
    if case.root_node == "connection_storm":
        return SQLAction(action_id=f"{case.case_id}-root", target_node=case.root_node, database=database, sql="SELECT SLEEP(5)", concurrency=3, execution_mode="concurrent", duration_sec=5, timeout_sec=15)
    if case.root_node == "hot_update":
        return SQLAction(action_id=f"{case.case_id}-root", target_node=case.root_node, database=database, sql="UPDATE warehouse SET w_ytd = w_ytd + 0.01 WHERE w_id = 1", concurrency=3, execution_mode="concurrent", duration_sec=10, timeout_sec=30)
    if case.root_node == "improper_sql":
        return SQLAction(action_id=f"{case.case_id}-root", target_node=case.root_node, database=database, sql="SELECT SQL_BIG_RESULT s_i_id, COUNT(*) FROM stock GROUP BY s_i_id ORDER BY COUNT(*) DESC", duration_sec=10, timeout_sec=30)
    if case.root_node == "large_temp_table":
        return SQLAction(action_id=f"{case.case_id}-root", target_node=case.root_node, database=database, sql="SELECT SQL_BIG_RESULT s_data, COUNT(*) FROM stock GROUP BY s_data ORDER BY COUNT(*) DESC", duration_sec=10, timeout_sec=30)
    if case.root_node == "long_tx":
        return TransactionAction(action_id=f"{case.case_id}-root", target_node=case.root_node, database=database, actors=[TransactionActor(actor_id="holder", role="holder", steps=[TransactionStep(sql="BEGIN"), TransactionStep(sql="SELECT w_id FROM warehouse WHERE w_id=1 FOR UPDATE", delay_after_sec=10), TransactionStep(sql="ROLLBACK")])], duration_sec=15, timeout_sec=30)
    if case.root_node == "metadata_lock":
        return TransactionAction(action_id=f"{case.case_id}-root", target_node=case.root_node, database=database, actors=[TransactionActor(actor_id="metadata-holder", role="holder", steps=[TransactionStep(sql="LOCK TABLES warehouse WRITE"), TransactionStep(sql="SELECT COUNT(*) FROM warehouse", delay_after_sec=5), TransactionStep(sql="UNLOCK TABLES")])], duration_sec=10, timeout_sec=30)
    if case.root_node == "table_lock":
        return TransactionAction(action_id=f"{case.case_id}-root", target_node=case.root_node, database=database, actors=[TransactionActor(actor_id="table-holder", role="holder", steps=[TransactionStep(sql="LOCK TABLES warehouse WRITE"), TransactionStep(sql="SELECT COUNT(*) FROM warehouse", delay_after_sec=5), TransactionStep(sql="UNLOCK TABLES")])], duration_sec=10, timeout_sec=30)
    raise ValueError(f"no SQL root factory for {case.root_node}")


def _root_action(case: ChainCase, database: str, env: dict[str, str], output_root: Path = OUTPUT_ROOT, profile: ExperimentProfile | None = None):
    if case.root_node in {"resource_cpu", "resource_memory"}:
        blade = env.get("DBMAGS_CHAOSBLADE_PATH", ".tools/chaosblade-1.8.0-darwin_arm64/blade")
        resource = "cpu" if case.root_node == "resource_cpu" else "memory"
        command = "cpu load --cpu-percent 80 --timeout 20" if resource == "cpu" else "mem load --mode ram --mem-percent 30 --timeout 20"
        return ChaosBladeAction(action_id=f"{case.case_id}-root", target_node=case.root_node, resource=resource, command=command, duration_sec=20, blade_path=blade)
    if case.root_node == "traffic_surge":
        return _benchbase_action(case, database, env, output_root=output_root, profile=profile)
    return _sql_root(case, database)


def _benchbase_action(case: ChainCase, database: str, env: dict[str, str], *, output_root: Path = OUTPUT_ROOT, profile: ExperimentProfile | None = None) -> BenchBaseAction:
    source = profile.benchbase.config_path if profile and profile.benchbase.config_path else REPO_ROOT / ".tools/benchbase-main/target/benchbase-mysql/config/mysql/sample_tpcc_config.xml"
    if not source.is_file():
        raise FileNotFoundError(f"BenchBase TPCC config does not exist: {source}")
    destination = output_root / case.case_id / "tpcc.xml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding="utf-8")
    url = html.escape(
        f"jdbc:mysql://{env.get('DBMAGS_MYSQL_HOST', '127.0.0.1')}:{env.get('DBMAGS_MYSQL_PORT', '3306')}/{database}?"
        "rewriteBatchedStatements=true&allowPublicKeyRetrieval=true&sslMode=DISABLED"
    )
    text = re.sub(r"<url>.*?</url>", f"<url>{url}</url>", text, count=1, flags=re.DOTALL)
    text = re.sub(r"<username>.*?</username>", f"<username>{html.escape(env.get('DBMAGS_MYSQL_USER', 'root'))}</username>", text, count=1, flags=re.DOTALL)
    text = re.sub(r"<password>.*?</password>", f"<password>{html.escape(env.get('DBMAGS_MYSQL_PASSWORD', ''))}</password>", text, count=1, flags=re.DOTALL)
    destination.write_text(text, encoding="utf-8")
    return BenchBaseAction(
        action_id=f"{case.case_id}-root",
        target_node="traffic_surge",
        benchmark=profile.workload.benchmark if profile else "tpcc",
        database=database,
        terminals=profile.workload.terminals if profile else 3,
        rate=profile.workload.rate if profile else 10,
        duration_sec=profile.workload.duration_seconds if profile else 60,
        config_path=str(destination),
        jar_path=str(profile.benchbase.jar_path if profile else REPO_ROOT / ".tools/benchbase-main/target/benchbase-mysql/benchbase.jar"),
        results_dir=str(output_root / case.case_id / "benchbase-results"),
        java_bin=profile.benchbase.java_bin if profile else "java",
    )


def _observations(timeline, case: ChainCase, *, adapter: MySQLAdapter | None = None, action: Any | None = None) -> list[NodeObservation]:
    samples = timeline.samples
    baseline = [sample for sample in samples if sample.phase.value == "baseline"]
    active = [sample for sample in samples if sample.phase.value in {"injection", "observing", "recovery"}]
    spill_active = [sample for sample in samples if sample.phase.value in {"injection", "observing"}]

    def values(name: str, source):
        return [sample.value for sample in source if sample.metric_name == name or sample.metric_name.endswith(f".{name}")]

    def mean(name: str, source):
        current = values(name, source)
        return sum(current) / len(current) if current else 0.0

    def ratio(name: str) -> float:
        base = mean(name, baseline)
        return mean(name, active) / base if base else 0.0

    def delta(name: str) -> float:
        current = values(name, active)
        base = values(name, baseline)
        return (current[-1] - base[0]) if current and base else 0.0

    def window_delta(name: str) -> float:
        current = values(name, spill_active)
        base = values(name, baseline)
        return max(0.0, current[-1] - base[-1]) if current and base else 0.0

    def window_increment_ratio(name: str) -> float:
        active_delta = window_delta(name)
        baseline_values = values(name, baseline)
        baseline_delta = max(0.0, baseline_values[-1] - baseline_values[0]) if len(baseline_values) >= 2 else 0.0
        return active_delta / baseline_delta if baseline_delta > 0 else 0.0

    def max_value(name: str, source) -> float:
        return max(values(name, source), default=0.0)

    baseline_buffer_pool = summarize_buffer_pool_window(
        samples,
        {ExperimentPhase.BASELINE},
    )
    active_buffer_pool = summarize_buffer_pool_window(
        samples,
        {ExperimentPhase.INJECTION, ExperimentPhase.OBSERVING},
    )

    def explain_metrics() -> dict[str, float | str | bool]:
        if adapter is None or action is None or not hasattr(action, "sql"):
            return {}
        result = adapter.explain_sql(action.sql)
        if not result.success or not result.data:
            return {}
        row = result.data[0]
        return {
            "explain_access_type": str(row.get("type", row.get("access_type", ""))),
            "explain_extra": str(row.get("Extra", row.get("extra", ""))),
        }

    observed: list[NodeObservation] = []
    for index, node in enumerate(case.chain[1:], start=1):
        metrics: dict[str, float | str | bool] = {
            "qps_ratio": ratio("qps"),
            "row_lock_waits_delta": delta("row_lock_waits"),
            "row_lock_current_waits": max_value("row_lock_current_waits", active),
            "row_lock_wait_time_ms_delta": delta("row_lock_wait_time_ms"),
            "threads_connected_ratio": ratio("threads_connected"),
            "lock_waits_delta": delta("lock_waits"),
            "lock_waits_current": max(values("lock_waits_current", active), default=0.0),
            "slow_queries_delta": delta("slow_queries_total"),
            "connection_error_delta": delta("connection_errors_total"),
            "timeout_error_count": 0.0,
            "metadata_lock_wait_count": max_value("metadata_lock_wait_count", active),
            "table_blocking_wait_count": max_value("table_blocking_wait_count", active),
            "table_blocking_current_count": max_value("table_blocking_current_count", active),
            "created_tmp_disk_tables_delta": window_delta("created_tmp_disk_tables"),
            "sort_merge_passes_delta": window_delta("sort_merge_passes"),
            "temp_table_ratio": window_increment_ratio("created_tmp_disk_tables"),
            "sort_merge_passes_ratio": window_increment_ratio("sort_merge_passes"),
            "innodb_buffer_pool_reads_delta": delta("innodb_buffer_pool_reads"),
            "deadlock_delta": delta("innodb_deadlocks"),
            "io_wait_ratio": ratio("io_wait_ratio"),
            "disk_util": max_value("disk_util", active),
            "cpu_usage_ratio": ratio("cpu_percent"),
            "memory_usage_ratio": ratio("memory_percent"),
            "buffer_pool_miss_ratio": active_buffer_pool["miss_ratio"],
            "buffer_pool_miss_ratio_valid": active_buffer_pool["miss_ratio_valid"],
            "buffer_pool_baseline_miss_ratio": baseline_buffer_pool["miss_ratio"],
            "buffer_pool_baseline_miss_ratio_valid": baseline_buffer_pool["miss_ratio_valid"],
            "buffer_pool_physical_reads_per_sec": active_buffer_pool["physical_reads_per_sec"],
            "buffer_pool_baseline_physical_reads_per_sec": baseline_buffer_pool["physical_reads_per_sec"],
            "buffer_pool_physical_reads_valid": active_buffer_pool["physical_reads_valid"],
            "buffer_pool_baseline_physical_reads_valid": baseline_buffer_pool["physical_reads_valid"],
            "innodb_log_waits_delta": delta("innodb_log_waits"),
            "tps_ratio": ratio("tps"),
        }
        metrics.update(explain_metrics())
        timestamp = float(index * max(1, len(active) // max(1, len(case.chain) - 1)))
        observed.append(NodeObservation(node_id=node, timestamp_sec=timestamp, metrics=metrics))
    return observed


def _write_result(case: ChainCase, payload: dict[str, Any], output_root: Path) -> Path:
    destination = output_root / case.case_id
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if isinstance(payload.get("timeline"), list):
        with (destination / "metrics.jsonl").open("w", encoding="utf-8") as stream:
            for record in payload["timeline"]:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return destination


def run_case(case: ChainCase, *, run_real: bool, include_risky: bool, output_root: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {"case_id": case.case_id, "chain": list(case.chain), "declared_status": case.status, "reason": case.reason, "started_at": time.time()}
    profile = load_experiment_profile(PROFILE_PATH)
    payload["experiment_profile"] = str(PROFILE_PATH)
    if case.status == "skipped":
        payload.update(status="skipped", skip_reason=case.reason)
        _write_result(case, payload, output_root)
        return payload
    if case.risky and not include_risky:
        payload.update(status="skipped", skip_reason="risky case requires --include-risky")
        _write_result(case, payload, output_root)
        return payload
    if not run_real:
        try:
            action = _root_action(case, _config(_dotenv()).database, _dotenv(), output_root, profile)
            payload.update(status="planned", root_action=action.model_dump(mode="json"))
        except Exception as exc:
            payload.update(status="planned", planning_error=str(exc))
        _write_result(case, payload, output_root)
        return payload

    env = _dotenv()
    if run_real:
        config = _config(env)
        try:
            with socket.create_connection((config.host, config.port), timeout=1):
                pass
        except OSError as exc:
            payload.update(status="environment_error", error_type=type(exc).__name__, error=str(exc))
            _write_result(case, payload)
            return payload
    try:
        config = _config(env)
        adapter = MySQLAdapter(config)
        preflight = run_preflight(adapter, profile, chaosblade_path=env.get("DBMAGS_CHAOSBLADE_PATH"))
        payload["preflight"] = preflight.model_dump(mode="json")
        if not preflight.passed:
            payload.update(status="environment_error", error_type="PreflightFailed", error="experiment preflight failed")
            _write_result(case, payload, output_root)
            return payload
        instrumentation = MySQLInstrumentation(adapter)
        instrumentation_state = instrumentation.prepare(profile.instrumentation)
        try:
            action = _root_action(case, config.database, env, output_root, profile)
            collector = MetricsCollector({"mysql": MySQLMetricsProvider(adapter), "system": SystemMetricsProvider()})
            runner = PropagationRunner(dispatcher=ActionDispatcher(adapter), sampler=MetricsSampler(collector, interval_seconds=profile.experiment.sample_interval_seconds))
            background = None if case.root_node == "traffic_surge" else _benchbase_action(case, config.database, env, output_root=output_root, profile=profile)
            request = PropagationReproductionRequest(incident_id=f"human-{case.case_id}", graph=_graph(case), root_action=action, background_workload=background, baseline_seconds=profile.experiment.baseline_seconds, observe_seconds=profile.experiment.observe_seconds or profile.experiment.injection_seconds, recovery_seconds=profile.experiment.recovery_seconds)
            result = runner.run(request, observation_provider=lambda timeline: _observations(timeline, case, adapter=adapter, action=action))
            payload.update(status=result.status.value, result=result.model_dump(mode="json", exclude={"timeline"}), timeline=result.timeline.records())
        finally:
            instrumentation.restore(instrumentation_state)
    except Exception as exc:
        payload.update(status="system_error", error_type=type(exc).__name__, error=str(exc))
    _write_result(case, payload, output_root)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--case")
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--list", action="store_true")
    parser.add_argument("--run-real", action="store_true")
    parser.add_argument("--include-risky", action="store_true")
    args = parser.parse_args()
    if args.list:
        for case in CASES:
            print(f"{case.case_id}\t{case.status}\t{' -> '.join(case.chain)}")
        return 0
    if not args.all and args.case not in CASE_BY_ID:
        parser.error(f"unknown case: {args.case}")
    selected = CASES if args.all else [CASE_BY_ID[args.case]]
    output_root = OUTPUT_ROOT / datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    results = [run_case(case, run_real=args.run_real, include_risky=args.include_risky, output_root=output_root) for case in selected]
    summary = {"run_at": time.time(), "results": results}
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"output": str(output_root), "counts": {status: sum(item["status"] == status for item in results) for status in sorted({item["status"] for item in results})}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
