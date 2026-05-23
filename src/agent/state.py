from typing import TypedDict

class AgentState(TypedDict, total=False):
    task_id: str
    alert: dict
    category: str
    urgency: str
    runbook_context: str
    log_context: str
    metrics_context: str
    diagnosis: str
    recommended_actions: list[str]
    auto_remediation: bool
    status: str
    audit_trail: list[dict]
    messages: list
