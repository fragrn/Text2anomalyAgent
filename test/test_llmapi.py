"""Real LLM API integration test for M10 ActionPlan generation.

This test never executes the generated database Action. It only verifies the
configuration, SDK call, Responses-first/fallback client behavior, and strict
ActionPlan parsing.
"""

from __future__ import annotations

import os

import pytest

from db_repro_agent.agent.action_generator import ActionGenerator
from db_repro_agent.agent.context_builder import ContextBuilder
from db_repro_agent.config import load_config
from db_repro_agent.models.action import (
    BenchBaseAction,
    ChaosBladeAction,
    SQLAction,
    TransactionAction,
)


pytestmark = pytest.mark.integration


def _llm_context():
    return ContextBuilder().build(
        target_anomaly="slow_query",
        target_node="slow_query",
        database="tpcc10_test",
        schema={
            "warehouse": {
                "columns": ["w_id", "w_ytd", "w_tax", "w_name"],
            }
        },
        indexes={"warehouse": ["PRIMARY(w_id)"]},
        data_scale={"warehouse": 1000},
        runtime_snapshot={"threads_running": 1, "threads_connected": 2},
        workload_status={"benchmark": "tpcc", "alive": True},
        duration_budget_seconds=15,
        safety_constraints={
            "max_duration_seconds": 15,
            "max_concurrency": 8,
            "forbidden_operations": ["DROP DATABASE", "TRUNCATE"],
        },
    )


def test_real_llm_api_generates_action_plan() -> None:
    """Call the configured gateway only when explicitly requested."""
    if os.environ.get("DBMAGS_RUN_LLM_API_TEST") != "1":
        pytest.skip("set DBMAGS_RUN_LLM_API_TEST=1 to call the real LLM API")

    config = load_config()
    missing = [
        name
        for name, value in (
            ("OPENAI_API_KEY", config.llm.api_key),
            ("OPENAI_BASE_URL", config.llm.base_url),
            ("OPENAI_MODEL", config.llm.model),
        )
        if not value
    ]
    if missing:
        pytest.fail(f"LLM configuration is missing: {', '.join(missing)}")

    generator = ActionGenerator.from_config(config.llm)
    try:
        plan = generator.generate(_llm_context())
    except Exception as exc:
        # Do not include the API key or the raw request/response in diagnostics.
        safe_message = str(exc).replace(config.llm.api_key, "<redacted>")
        pytest.fail(
            "LLM API call or ActionPlan parsing failed: "
            f"base_url={config.llm.base_url}, model={config.llm.model}, "
            f"error_type={type(exc).__name__}, error={safe_message}"
        )

    assert isinstance(plan.action, (SQLAction, TransactionAction, BenchBaseAction, ChaosBladeAction))
    assert plan.action.action_id
    assert plan.action.target_node

