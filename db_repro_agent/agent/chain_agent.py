"""M10 Chain Agent: generate a validated ActionPlan from a fixed chain."""

from __future__ import annotations

from .action_generator import ActionGenerator, ActionPlan
from .context_builder import ActionGenerationContext, ContextBuilder
from .contracts import ChainReproductionRequest, ExperimentResult
from ..runtime.action_validator import ActionValidator
from ..runtime.safety import SafetyChecker


class ChainAgent:
    """Shared Action-generation entry point for a human-specified chain.

    M10 stops after ActionPlan generation and validation. Actual propagation
    execution remains the responsibility of the M12/M9 reproduction pipeline.
    """

    def __init__(
        self,
        action_generator: ActionGenerator,
        *,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self.action_generator = action_generator
        self.context_builder = context_builder or ContextBuilder()

    def build_context(self, request: ChainReproductionRequest) -> ActionGenerationContext:
        return self.context_builder.build(
            target_anomaly=request.target_node,
            target_node=request.target_node,
            database=request.database,
            schema=request.schema_,
            indexes=request.indexes,
            data_scale=request.data_scale,
            runtime_snapshot=request.runtime_snapshot,
            workload_status=request.workload_status,
            propagation_chain=request.propagation_chain,
            active_injection_nodes=request.active_injection_nodes,
            observed_downstream_nodes=request.observed_downstream_nodes,
            duration_budget_seconds=request.duration_budget_seconds,
            safety_constraints=request.safety_constraints,
            previous_reflection=request.previous_reflection,
        )

    def generate_action_plan(self, request: ChainReproductionRequest) -> ActionPlan:
        return self.action_generator.generate(self.build_context(request))

    def reproduce_chain(
        self,
        request: ChainReproductionRequest,
        *,
        validator: ActionValidator,
        safety_checker: SafetyChecker,
    ) -> ExperimentResult:
        plan = self.generate_action_plan(request)
        validation = self.action_generator.validate_for_execution(
            plan, validator=validator, safety_checker=safety_checker
        )
        if not validation.passed:
            return ExperimentResult(
                incident_id=request.incident_id,
                agent="chain",
                status="rejected",
                action_plan=plan,
                validation=validation,
                message="ActionPlan rejected before execution",
            )
        return ExperimentResult(
            incident_id=request.incident_id,
            agent="chain",
            status="action_generated",
            action_plan=plan,
            validation=validation,
            message="M10 generated and validated an ActionPlan; execution is deferred",
        )
