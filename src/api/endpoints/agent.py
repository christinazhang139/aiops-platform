from fastapi import APIRouter, Request
from pydantic import BaseModel
from src.agent.workflows import OpsAgent
from src.monitoring.incidents import get_incident_store
from src.executor.engine import ExecutionEngine

router = APIRouter()

class DiagnoseRequest(BaseModel):
    service: str
    symptoms: str
    time_range: str = "1h"


def _determine_auto_action(diagnosis: str, service: str) -> dict | None:
    """Based on diagnosis, determine if we can auto-remediate (low risk only)."""
    diag_lower = diagnosis.lower()

    if "oom" in diag_lower or "memory" in diag_lower or "killed" in diag_lower:
        return {"action": "restart_pod", "service": service, "risk": "low", "reason": "OOM detected, restarting pod"}
    if "crash" in diag_lower and "loop" in diag_lower:
        return {"action": "restart_pod", "service": service, "risk": "low", "reason": "CrashLoop detected, restarting pod"}
    return None


@router.post("/alert")
async def handle_alert(request: Request):
    """Accept alerts, diagnose, and auto-remediate low-risk issues."""
    body = await request.json()
    agent = OpsAgent()
    store = get_incident_store()

    # AlertManager format
    if "alerts" in body:
        results = []
        for alert in body["alerts"]:
            if alert.get("status") != "firing":
                continue
            alert_data = {
                "alert_name": alert.get("labels", {}).get("alertname", "unknown"),
                "severity": alert.get("labels", {}).get("severity", "warning"),
                "service": alert.get("labels", {}).get("service", "unknown"),
                "description": alert.get("annotations", {}).get("summary", ""),
                "labels": alert.get("labels", {}),
            }
            result = await _process_alert(agent, store, alert_data)
            results.append(result)
        return {"processed": len(results), "results": results}

    # Our format
    alert_data = {
        "alert_name": body.get("alert_name", "unknown"),
        "severity": body.get("severity", "warning"),
        "service": body.get("service", "unknown"),
        "description": body.get("description", ""),
        "labels": body.get("labels", {}),
    }
    return await _process_alert(agent, store, alert_data)


async def _process_alert(agent: OpsAgent, store, alert_data: dict) -> dict:
    """Process a single alert: diagnose -> auto-remediate if low risk."""
    service = alert_data.get("service", "unknown")
    namespace = alert_data.get("labels", {}).get("namespace", "demo-project")

    # Step 1: AI diagnosis
    result = await agent.handle_alert(alert_data)
    diagnosis = result.get("diagnosis", "")

    # Record incident
    store.record(
        type="alert",
        service=service,
        namespace=namespace,
        summary=f"Alert: {alert_data.get('alert_name', '')} - {alert_data.get('description', '')}",
        details=diagnosis[:500],
    )

    # Step 2: Determine if auto-remediation is possible
    auto_action = _determine_auto_action(diagnosis, service)

    if auto_action:
        engine = ExecutionEngine()
        exec_result = await engine.execute(
            action=auto_action["action"],
            service=service,
            namespace=namespace,
        )

        if exec_result.get("success"):
            store.record(
                type="action",
                service=service,
                namespace=namespace,
                summary=f"Auto-remediation: {auto_action['action']} ({auto_action['reason']})",
                details=exec_result.get("message", ""),
                actions=[auto_action["action"]],
            )
            result["auto_remediation"] = {
                "executed": True,
                "action": auto_action["action"],
                "reason": auto_action["reason"],
                "result": exec_result.get("message"),
            }
        else:
            result["auto_remediation"] = {
                "executed": False,
                "action": auto_action["action"],
                "reason": f"Failed: {exec_result.get('message', 'unknown error')}",
            }
    else:
        result["auto_remediation"] = {"executed": False, "reason": "No low-risk auto-action identified"}

    return result


@router.post("/diagnose")
async def diagnose(req: DiagnoseRequest):
    agent = OpsAgent()
    store = get_incident_store()
    result = await agent.diagnose(req.service, req.symptoms, req.time_range)

    store.record(
        type="diagnosis",
        service=req.service,
        namespace="demo-project",
        summary=f"Manual diagnosis: {req.symptoms}",
        details=result.get("diagnosis", "")[:500],
    )

    return result
