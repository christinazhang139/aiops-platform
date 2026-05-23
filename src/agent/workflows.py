import uuid
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from src.config.settings import build_llm
from src.agent.state import AgentState
from src.agent.tools import AGENT_TOOLS
from src.agent.prompts import TRIAGE_SYSTEM, CLASSIFY_PROMPT, DIAGNOSE_PROMPT

_task_store: dict[str, dict] = {}

async def classify_node(state: AgentState) -> AgentState:
    llm = build_llm()
    alert = state.get("alert", {})
    prompt = CLASSIFY_PROMPT.format(
        alert_name=alert.get("alert_name",""), service=alert.get("service",""),
        severity=alert.get("severity",""), description=alert.get("description",""),
    )
    response = await llm.ainvoke([SystemMessage(content=TRIAGE_SYSTEM), HumanMessage(content=prompt)])
    _audit(state, "classify", response.content[:200])
    state["status"] = "investigating"
    state["messages"] = [SystemMessage(content=TRIAGE_SYSTEM), HumanMessage(content=prompt), response]
    return state

async def gather_and_diagnose_node(state: AgentState) -> AgentState:
    llm = build_llm()
    alert = state.get("alert", {})
    service = alert.get("service", "unknown")

    from src.agent.tools import search_runbook, query_logs
    runbook_result = await search_runbook.ainvoke({"query": f"{alert.get('alert_name','')} {alert.get('description','')}"})
    log_result = await query_logs.ainvoke({"service": service, "time_range": "1h"})

    prompt = DIAGNOSE_PROMPT.format(
        alert_name=alert.get("alert_name",""), service=service,
        runbook_context=runbook_result[:2000], log_context=log_result[:2000],
    )
    response = await llm.ainvoke(state.get("messages",[]) + [HumanMessage(content=prompt)])
    state["diagnosis"] = response.content
    state["recommended_actions"] = [response.content]
    state["status"] = "diagnosed"
    _audit(state, "diagnose", response.content[:200])
    return state

def _audit(state: AgentState, step: str, detail: str):
    trail = state.get("audit_trail", [])
    trail.append({"timestamp": datetime.now(timezone.utc).isoformat(), "step": step, "detail": detail})
    state["audit_trail"] = trail

def build_workflow() -> StateGraph:
    wf = StateGraph(AgentState)
    wf.add_node("classify", classify_node)
    wf.add_node("gather_and_diagnose", gather_and_diagnose_node)
    wf.set_entry_point("classify")
    wf.add_edge("classify", "gather_and_diagnose")
    wf.add_edge("gather_and_diagnose", END)
    return wf

class OpsAgent:
    def __init__(self):
        self.app = build_workflow().compile()

    async def handle_alert(self, alert_data: dict) -> dict:
        task_id = str(uuid.uuid4())
        state: AgentState = {"task_id": task_id, "alert": alert_data, "status": "received", "audit_trail": [], "recommended_actions": [], "messages": []}
        try:
            final = await self.app.ainvoke(state)
            result = {"task_id": task_id, "status": final.get("status","done"), "diagnosis": final.get("diagnosis"), "recommended_actions": final.get("recommended_actions",[]), "audit_trail": final.get("audit_trail",[])}
        except Exception as e:
            result = {"task_id": task_id, "status": "error", "diagnosis": str(e), "recommended_actions": [], "audit_trail": []}
        _task_store[task_id] = result
        return result

    async def diagnose(self, service: str, symptoms: str, time_range: str = "1h") -> dict:
        return await self.handle_alert({"alert_name": "manual_diagnosis", "severity": "info", "service": service, "description": symptoms})

    async def get_task_status(self, task_id: str) -> dict:
        return _task_store.get(task_id, {"task_id": task_id, "status": "not_found"})
