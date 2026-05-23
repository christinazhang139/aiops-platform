"""Monitoring API — namespace management, dashboard data, incident history."""

from fastapi import APIRouter
from pydantic import BaseModel
from src.monitoring.config import get_monitoring_config
from src.monitoring.health import check_all_services, format_health_report, check_service_health
from src.monitoring.incidents import get_incident_store

router = APIRouter()


class AddServiceRequest(BaseModel):
    name: str
    namespace: str
    label_selector: str = ""


class RemoveServiceRequest(BaseModel):
    name: str
    namespace: str


@router.get("/services")
async def list_monitored_services():
    """List all monitored services."""
    config = get_monitoring_config()
    return {"services": [{"name": s.name, "namespace": s.namespace, "label_selector": s.label_selector, "added_at": s.added_at} for s in config.get_all()]}


@router.post("/services")
async def add_monitored_service(req: AddServiceRequest):
    """Add a service to monitoring."""
    config = get_monitoring_config()
    svc = config.add_service(req.name, req.namespace, req.label_selector)
    return {"message": f"Added {req.name} in {req.namespace} to monitoring", "service": {"name": svc.name, "namespace": svc.namespace}}


@router.delete("/services")
async def remove_monitored_service(req: RemoveServiceRequest):
    """Remove a service from monitoring."""
    config = get_monitoring_config()
    removed = config.remove_service(req.name, req.namespace)
    if removed:
        return {"message": f"Removed {req.name} from monitoring"}
    return {"message": f"Service {req.name} not found in monitoring config"}


@router.get("/dashboard")
async def get_dashboard_data():
    """Get real-time dashboard data for all monitored services."""
    results = check_all_services()
    overall = "HEALTHY"
    for r in results:
        if r.status == "DOWN":
            overall = "DOWN"
            break
        elif r.status == "DEGRADED":
            overall = "DEGRADED"

    return {
        "overall_status": overall,
        "services": [
            {
                "name": r.name,
                "namespace": r.namespace,
                "status": r.status,
                "pods_running": r.pods_running,
                "pods_total": r.pods_total,
                "restarts": r.restarts,
                "memory": r.memory_usage,
                "recent_errors": r.recent_errors,
                "recommendations": r.recommendations,
            }
            for r in results
        ],
    }


@router.get("/incidents")
async def get_incidents(limit: int = 20):
    """Get recent incident history."""
    store = get_incident_store()
    incidents = store.get_recent(limit)
    return {"incidents": [i.to_dict() for i in incidents]}


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str):
    """Mark an incident as resolved."""
    store = get_incident_store()
    if store.resolve(incident_id):
        return {"message": f"Incident {incident_id} resolved"}
    return {"message": f"Incident {incident_id} not found"}


@router.get("/namespaces")
async def list_available_namespaces():
    """List namespaces available in the cluster (for the UI selector)."""
    import os, httpx
    try:
        token = open("/var/run/secrets/kubernetes.io/serviceaccount/token").read().strip()
        r = httpx.get(
            "https://kubernetes.default.svc/api/v1/namespaces",
            headers={"Authorization": f"Bearer {token}"},
            verify="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
            timeout=10,
        )
        data = r.json()
        namespaces = [
            ns["metadata"]["name"]
            for ns in data.get("items", [])
            if not ns["metadata"]["name"].startswith("openshift-")
            and not ns["metadata"]["name"].startswith("kube-")
            and ns["metadata"]["name"] not in ("default", "openshift")
        ]
        return {"namespaces": sorted(namespaces)}
    except Exception as e:
        return {"namespaces": [], "error": str(e)}
