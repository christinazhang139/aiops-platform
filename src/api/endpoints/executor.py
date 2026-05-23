from fastapi import APIRouter
from pydantic import BaseModel
from src.executor.engine import ExecutionEngine

router = APIRouter()

class ExecuteRequest(BaseModel):
    action: str
    service: str
    namespace: str = "demo-project"
    parameters: str = "{}"

class ApproveRequest(BaseModel):
    approval_id: str
    approved_by: str

@router.post("/execute")
async def execute_action(req: ExecuteRequest):
    engine = ExecutionEngine()
    return await engine.execute(req.action, req.service, req.namespace, req.parameters)

@router.post("/approve")
async def approve_action(req: ApproveRequest):
    engine = ExecutionEngine()
    return await engine.approve(req.approval_id, req.approved_by)

@router.get("/pending")
async def get_pending():
    engine = ExecutionEngine()
    return {"pending_approvals": engine.get_pending()}

@router.get("/audit")
async def get_audit():
    engine = ExecutionEngine()
    return {"audit_log": engine.get_audit_log()}
