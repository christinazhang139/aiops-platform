"""Comprehensive health check — Pod status, resource usage, recent errors."""

import os
import httpx
from dataclasses import dataclass
from src.monitoring.config import get_monitoring_config, MonitoredService

K8S_HOST = "https://kubernetes.default.svc"
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def _get_headers() -> dict:
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            token = f.read().strip()
        return {"Authorization": f"Bearer {token}"}
    return {}


def _k8s_get(path: str, params: dict = None) -> dict:
    verify = CA_PATH if os.path.exists(CA_PATH) else False
    r = httpx.get(f"{K8S_HOST}{path}", headers=_get_headers(), params=params, verify=verify, timeout=10)
    return r.json()


@dataclass
class ServiceHealth:
    name: str
    namespace: str
    status: str  # HEALTHY, DEGRADED, DOWN, UNKNOWN
    pods_running: int
    pods_total: int
    restarts: int
    cpu_usage: str
    memory_usage: str
    recent_errors: list[str]
    recommendations: list[str]


def check_service_health(svc: MonitoredService) -> ServiceHealth:
    """Full health check for a single service."""
    try:
        data = _k8s_get(f"/api/v1/namespaces/{svc.namespace}/pods", {"labelSelector": svc.label_selector})
        pods = data.get("items", [])
    except Exception as e:
        return ServiceHealth(name=svc.name, namespace=svc.namespace, status="UNKNOWN", pods_running=0, pods_total=0, restarts=0, cpu_usage="N/A", memory_usage="N/A", recent_errors=[str(e)], recommendations=["Check cluster connectivity"])

    total = len(pods)
    running = 0
    restarts = 0
    recent_errors = []
    memory_info = []

    for p in pods:
        phase = p.get("status", {}).get("phase", "Unknown")
        if phase == "Running":
            running += 1

        for cs in p.get("status", {}).get("containerStatuses", []):
            restarts += cs.get("restartCount", 0)
            
            last_state = cs.get("lastState", {})
            if "terminated" in last_state:
                reason = last_state["terminated"].get("reason", "Unknown")
                if reason == "OOMKilled":
                    recent_errors.append(f"Pod {p['metadata']['name']}: OOMKilled")
                elif reason == "Error":
                    recent_errors.append(f"Pod {p['metadata']['name']}: Crashed (exit code {last_state['terminated'].get('exitCode', '?')})")

        # Resource info from spec
        for c in p.get("spec", {}).get("containers", []):
            limits = c.get("resources", {}).get("limits", {})
            if limits.get("memory"):
                memory_info.append(limits["memory"])

    # Determine status
    if total == 0:
        status = "DOWN"
    elif running == 0:
        status = "DOWN"
    elif running < total or restarts > 0 or recent_errors:
        status = "DEGRADED"
    else:
        status = "HEALTHY"

    # Generate recommendations
    recs = []
    if restarts > 0:
        recs.append("Pod restarts detected — investigate OOM or crash issues")
    if running < total:
        recs.append(f"Only {running}/{total} pods running — check pod events")
    if any("OOMKilled" in e for e in recent_errors):
        recs.append("OOM Kill detected — consider increasing memory limits")
    if status == "HEALTHY":
        recs.append("No issues detected")

    memory_str = memory_info[0] if memory_info else "N/A"

    return ServiceHealth(
        name=svc.name, namespace=svc.namespace, status=status,
        pods_running=running, pods_total=total, restarts=restarts,
        cpu_usage="N/A", memory_usage=f"limit: {memory_str}",
        recent_errors=recent_errors, recommendations=recs,
    )


def check_all_services() -> list[ServiceHealth]:
    """Check health of all monitored services."""
    config = get_monitoring_config()
    return [check_service_health(svc) for svc in config.get_all()]


def format_health_report(results: list[ServiceHealth]) -> str:
    """Format health results into a readable report."""
    lines = ["Cluster Health Report", "=" * 50, ""]
    
    overall = "HEALTHY"
    for r in results:
        if r.status == "DOWN":
            overall = "DOWN"
            break
        elif r.status == "DEGRADED":
            overall = "DEGRADED"

    lines.append(f"Overall Status: {overall}")
    lines.append(f"Monitored Services: {len(results)}")
    lines.append("")

    for r in results:
        icon = {"HEALTHY": "[OK]", "DEGRADED": "[WARN]", "DOWN": "[CRIT]", "UNKNOWN": "[?]"}.get(r.status, "[?]")
        lines.append(f"{icon} {r.name} ({r.namespace})")
        lines.append(f"    Status: {r.status}")
        lines.append(f"    Pods: {r.pods_running}/{r.pods_total} Running")
        lines.append(f"    Restarts: {r.restarts}")
        lines.append(f"    Memory: {r.memory_usage}")
        if r.recent_errors:
            lines.append(f"    Recent Issues:")
            for e in r.recent_errors[:3]:
                lines.append(f"      - {e}")
        if r.recommendations and r.recommendations[0] != "No issues detected":
            lines.append(f"    Recommendations:")
            for rec in r.recommendations:
                lines.append(f"      - {rec}")
        lines.append("")

    return "\n".join(lines)
