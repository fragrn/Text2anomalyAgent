"""M10 Post Agent interface; incident analysis begins in M13."""

from __future__ import annotations

from .contracts import ExperimentResult, PostReproductionRequest


class PostAgent:
    """Stable placeholder for the future DBA Post reproduction pipeline."""

    def reproduce_post(self, request: PostReproductionRequest) -> ExperimentResult:
        return ExperimentResult(
            incident_id=request.incident_id,
            agent="post",
            status="not_implemented",
            message="Post analysis and Direct/Propagation selection start in M13",
        )
