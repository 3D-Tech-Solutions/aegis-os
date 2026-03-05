# Copyright 2026 Tim Escolopio / 3D Tech Solutions
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Control Plane Router - decides which agent handles which sub-task.

All LLM execution MUST be routed through ``orchestrator.run()``.  Governance
modules (Guardrails, OPA, SessionManager) must never be called directly from
this module – they are the orchestrator's exclusive responsibility.
"""

from enum import StrEnum
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.audit_vault.logger import AuditLogger
from src.control_plane.orchestrator import (
    BudgetLimitError,
    Orchestrator,
    OrchestratorRequest,
    OrchestratorResult,
)

router = APIRouter(tags=["control-plane"])
_logger = AuditLogger()

# Module-level orchestrator instance – set at application startup via
# ``configure_orchestrator()``.  Tests replace this with a mock.
_orchestrator: Orchestrator | None = None


def configure_orchestrator(orc: Orchestrator) -> None:
    """Inject the orchestrator instance (called during app startup)."""
    global _orchestrator  # noqa: PLW0603
    _orchestrator = orc


def _require_orchestrator() -> Orchestrator:
    if _orchestrator is None:
        raise RuntimeError(
            "Orchestrator has not been configured.  "
            "Call configure_orchestrator() during app startup."
        )
    return _orchestrator


class AgentType(StrEnum):
    FINANCE = "finance"
    HR = "hr"
    IT = "it"
    LEGAL = "legal"
    GENERAL = "general"


class TaskRequest(BaseModel):
    """Incoming agent task — carries everything the orchestrator pipeline needs."""

    task_id: UUID = Field(default_factory=uuid4)
    prompt: str = Field(..., min_length=1, max_length=32_768)
    description: str = Field(default="", max_length=4096)
    agent_type: AgentType = AgentType.GENERAL
    requester_id: str = Field(..., min_length=1, max_length=256)
    model: str = "gpt-4o-mini"
    max_tokens: int = 1024
    temperature: float = 0.7
    system_prompt: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    """Unified response returned from POST /tasks after full pipeline execution."""

    task_id: UUID
    agent_type: AgentType
    session_token: str
    message: str
    tokens_used: int = 0
    model: str = ""
    pii_found: list[str] = Field(default_factory=list)


class ExecuteRequest(BaseModel):
    """Request body for the LLM execution endpoint."""

    task_id: UUID = Field(default_factory=uuid4)
    prompt: str = Field(..., min_length=1, max_length=32_768)
    agent_type: AgentType = AgentType.GENERAL
    requester_id: str = Field(..., min_length=1, max_length=256)
    session_token: str | None = None
    model: str = "gpt-4o-mini"
    max_tokens: int = 1024
    temperature: float = 0.7
    system_prompt: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)


@router.post("/tasks", response_model=TaskResponse)
async def route_task(request: TaskRequest) -> TaskResponse:
    """Execute a task through the full governance pipeline and return the LLM result.

    All governance enforcement (PII scrubbing, OPA policy evaluation, JIT session
    token issuance) is performed exclusively by the orchestrator.  This handler
    must not call any governance module directly.
    """
    orc = _require_orchestrator()

    _logger.info(
        "task.routing",
        task_id=str(request.task_id),
        agent_type=request.agent_type,
        requester_id=request.requester_id,
    )

    try:
        result: OrchestratorResult = await orc.run(
            OrchestratorRequest(
                task_id=request.task_id,
                prompt=request.prompt,
                agent_type=request.agent_type.value,
                requester_id=request.requester_id,
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system_prompt=request.system_prompt,
                metadata=request.metadata,
            )
        )
    except BudgetLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _logger.error(
            "task.error",
            task_id=str(request.task_id),
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="Internal pipeline error") from exc

    _logger.info(
        "task.routed",
        task_id=str(request.task_id),
        agent_type=request.agent_type,
    )

    return TaskResponse(
        task_id=result.task_id,
        agent_type=request.agent_type,
        session_token=result.session_token,
        message=result.response.content,
        tokens_used=result.response.tokens_used,
        model=result.response.model,
        pii_found=list(
            dict.fromkeys(result.pii_found_in_prompt + result.pii_found_in_response)
        ),
    )


@router.post("/tasks/execute", response_model=OrchestratorResult)
async def execute_task(request: ExecuteRequest) -> OrchestratorResult:
    """Execute a task through the full orchestration pipeline.

    All governance enforcement (PII scrubbing, policy evaluation, session token
    issuance) is handled exclusively by the orchestrator.  This handler must
    not call any governance module directly.
    """
    orc = _require_orchestrator()

    _logger.info(
        "task.execute",
        task_id=str(request.task_id),
        agent_type=request.agent_type,
        requester_id=request.requester_id,
    )

    try:
        result: OrchestratorResult = await orc.run(
            OrchestratorRequest(
                task_id=request.task_id,
                prompt=request.prompt,
                agent_type=request.agent_type.value,
                requester_id=request.requester_id,
                session_token=request.session_token,
                model=request.model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                system_prompt=request.system_prompt,
                metadata=request.metadata,
            )
        )
    except BudgetLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _logger.error(
            "task.execute.error",
            task_id=str(request.task_id),
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="Internal pipeline error") from exc

    return result


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: UUID) -> dict[str, str]:
    """Get the status of a routed task."""
    return {"task_id": str(task_id), "status": "pending"}
