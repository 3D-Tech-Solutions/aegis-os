"""Control Plane Router - decides which agent handles which sub-task."""

from enum import StrEnum
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.audit_vault.logger import AuditLogger
from src.governance.session_mgr import SessionManager

router = APIRouter(tags=["control-plane"])
_logger = AuditLogger()
_session_mgr = SessionManager()


class AgentType(StrEnum):
    FINANCE = "finance"
    HR = "hr"
    IT = "it"
    LEGAL = "legal"
    GENERAL = "general"


class TaskRequest(BaseModel):
    task_id: UUID = Field(default_factory=uuid4)
    description: str = Field(..., min_length=1, max_length=4096)
    agent_type: AgentType = AgentType.GENERAL
    requester_id: str = Field(..., min_length=1, max_length=256)
    metadata: dict[str, str] = Field(default_factory=dict)


class TaskResponse(BaseModel):
    task_id: UUID
    agent_type: AgentType
    session_token: str
    message: str


@router.post("/tasks", response_model=TaskResponse)
async def route_task(request: TaskRequest) -> TaskResponse:
    """Route an incoming task to the appropriate agent and issue a scoped session token."""
    _logger.info(
        "task.routing",
        task_id=str(request.task_id),
        agent_type=request.agent_type,
        requester_id=request.requester_id,
    )

    try:
        token = _session_mgr.issue_token(
            agent_type=request.agent_type.value,
            requester_id=request.requester_id,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _logger.info(
        "task.routed",
        task_id=str(request.task_id),
        agent_type=request.agent_type,
    )

    return TaskResponse(
        task_id=request.task_id,
        agent_type=request.agent_type,
        session_token=token,
        message=f"Task routed to {request.agent_type.value} agent",
    )


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: UUID) -> dict[str, str]:
    """Get the status of a routed task."""
    return {"task_id": str(task_id), "status": "pending"}
