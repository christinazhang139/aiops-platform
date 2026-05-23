import re
from src.rag.retriever import RAGRetriever
from src.agent.workflows import OpsAgent
from src.log_analyzer.analyzer import LogAnalyzer

COMMANDS = {
    "ask": re.compile(r"^/ops\s+ask\s+(.+)", re.I),
    "diagnose": re.compile(r"^/ops\s+diagnose\s+(\S+)(?:\s+(.+))?", re.I),
    "logs": re.compile(r"^/ops\s+logs\s+(\S+)(?:\s+(\S+))?", re.I),
    "health": re.compile(r"^/ops\s+health", re.I),
    "restart": re.compile(r"^/ops\s+restart\s+(\S+)", re.I),
    "scale": re.compile(r"^/ops\s+scale\s+(\S+)\s+(\d+)", re.I),
    "scale_memory": re.compile(r"^/ops\s+scale-memory\s+(\S+)\s+(\S+)", re.I),
    "rollback": re.compile(r"^/ops\s+rollback\s+(\S+)", re.I),
    "approve": re.compile(r"^/ops\s+approve\s+(\S+)", re.I),
    "help": re.compile(r"^/ops\s+help", re.I),
}

HELP = """/ops health — Check cluster and services health status
/ops ask <question> — Query knowledge base
/ops diagnose <service> [symptoms] — AI-powered diagnosis
/ops logs <service> [time_range] — Analyze service logs
/ops restart <service> — Restart a service (low risk, auto-execute)
/ops scale <service> <replicas> — Scale service (medium risk, needs approval)
/ops approve <id> — Approve a pending action
/ops help — Show this help

You can also ask questions in natural language."""

class ChatOpsHandler:
    def __init__(self):
        self.retriever = RAGRetriever()
        self.agent = OpsAgent()
        self.analyzer = LogAnalyzer()

    async def handle_message(self, message: str, user_id: str = "", channel: str = "") -> dict:
        for name, pattern in COMMANDS.items():
            m = pattern.match(message.strip())
            if m:
                return await getattr(self, f"_cmd_{name}")(m)
        return await self._natural_language(message)

    async def _cmd_health(self, m) -> dict:
        """Comprehensive health check for all monitored services."""
        try:
            from src.monitoring.health import check_all_services, format_health_report
            from src.monitoring.incidents import get_incident_store

            results = check_all_services()
            report = format_health_report(results)

            # Record in incident history
            store = get_incident_store()
            overall = "HEALTHY"
            for r in results:
                if r.status in ("DOWN", "DEGRADED"):
                    overall = r.status
                    store.record("health_check", r.name, r.namespace, f"Status: {r.status}, Restarts: {r.restarts}", details="; ".join(r.recent_errors))

            # Generate suggested actions based on findings
            suggested = []
            has_issues = False
            for r in results:
                if r.restarts > 0:
                    has_issues = True
                    suggested.append({"label": f"Diagnose {r.name}", "command": f"/ops diagnose {r.name} pod restarts detected", "risk": "low"})
                    suggested.append({"label": f"Restart {r.name}", "command": f"/ops restart {r.name}", "risk": "low"})
                    suggested.append({"label": f"Increase Memory ({r.name})", "command": f"/ops scale-memory {r.name} 512Mi", "risk": "medium"})
                if r.pods_running < r.pods_total:
                    has_issues = True
                    suggested.append({"label": f"Diagnose {r.name}", "command": f"/ops diagnose {r.name} pods not ready", "risk": "low"})
                    suggested.append({"label": f"Rollback {r.name}", "command": f"/ops rollback {r.name}", "risk": "medium"})
                if r.recent_errors:
                    has_issues = True
                    suggested.append({"label": f"Check Logs ({r.name})", "command": f"/ops logs {r.name} 1h", "risk": "low"})

            if not has_issues:
                return {"reply": report, "actions_taken": ["health_check"], "sources": []}

            # Deduplicate
            seen = set()
            unique_suggested = []
            for s in suggested:
                key = s["command"]
                if key not in seen:
                    seen.add(key)
                    unique_suggested.append(s)

            return {"reply": report, "actions_taken": ["health_check"], "sources": [], "suggested_actions": unique_suggested[:6]}

        except Exception as e:
            return {"reply": f"Health check failed: {str(e)}", "actions_taken": ["health_check"], "sources": []}

    async def _cmd_ask(self, m) -> dict:
        result = await self.retriever.query(m.group(1))
        return {"reply": result["answer"], "actions_taken": ["knowledge_query"], "sources": [s["source"] for s in result.get("sources",[])]}

    async def _cmd_diagnose(self, m) -> dict:
        service, symptoms = m.group(1), m.group(2) or "health check"
        result = await self.agent.diagnose(service, symptoms)
        diagnosis = result.get('diagnosis', 'N/A')
        reply = f"**Diagnosis for {service}**\n\n{diagnosis}"

        suggested = []
        diag_lower = diagnosis.lower()

        if "oom" in diag_lower or "memory" in diag_lower or "killed" in diag_lower:
            suggested = [
                {"label": "Increase Memory to 512Mi", "command": f"/ops scale-memory {service} 512Mi", "risk": "medium"},
                {"label": "Restart Pod", "command": f"/ops restart {service}", "risk": "low"},
                {"label": "Scale Up to 3 Replicas", "command": f"/ops scale {service} 3", "risk": "medium"},
                {"label": "Check Logs", "command": f"/ops logs {service} 1h", "risk": "low"},
            ]
        elif "connection" in diag_lower or "pool" in diag_lower or "timeout" in diag_lower or "database" in diag_lower:
            suggested = [
                {"label": "Restart Pod", "command": f"/ops restart {service}", "risk": "low"},
                {"label": "Scale Up to 3 Replicas", "command": f"/ops scale {service} 3", "risk": "medium"},
                {"label": "Check Logs", "command": f"/ops logs {service} 1h", "risk": "low"},
                {"label": "Query Runbook", "command": "/ops ask how to fix database connection pool exhaustion", "risk": "low"},
            ]
        elif "crash" in diag_lower or "loop" in diag_lower or "restart" in diag_lower:
            suggested = [
                {"label": "Check Crash Logs", "command": f"/ops logs {service} 30m", "risk": "low"},
                {"label": "Restart Pod", "command": f"/ops restart {service}", "risk": "low"},
                {"label": "Rollback Deployment", "command": f"/ops rollback {service}", "risk": "medium"},
                {"label": "Query Runbook", "command": "/ops ask how to fix CrashLoopBackOff", "risk": "low"},
            ]
        elif "latency" in diag_lower or "slow" in diag_lower or "performance" in diag_lower:
            suggested = [
                {"label": "Scale Up to 3 Replicas", "command": f"/ops scale {service} 3", "risk": "medium"},
                {"label": "Restart Pod", "command": f"/ops restart {service}", "risk": "low"},
                {"label": "Check Logs", "command": f"/ops logs {service} 1h", "risk": "low"},
            ]
        elif "error" in diag_lower or "500" in diag_lower or "rate" in diag_lower:
            suggested = [
                {"label": "Check Logs", "command": f"/ops logs {service} 1h", "risk": "low"},
                {"label": "Restart Pod", "command": f"/ops restart {service}", "risk": "low"},
                {"label": "Rollback Deployment", "command": f"/ops rollback {service}", "risk": "medium"},
                {"label": "Scale Up to 3 Replicas", "command": f"/ops scale {service} 3", "risk": "medium"},
            ]
        else:
            suggested = [
                {"label": "Check Logs", "command": f"/ops logs {service} 1h", "risk": "low"},
                {"label": "Restart Pod", "command": f"/ops restart {service}", "risk": "low"},
                {"label": "Health Check", "command": "/ops health", "risk": "low"},
            ]

        return {"reply": reply, "actions_taken": ["diagnosis"], "sources": [], "suggested_actions": suggested}

    async def _cmd_logs(self, m) -> dict:
        service, tr = m.group(1), m.group(2) or "1h"
        result = await self.analyzer.analyze(service, tr)
        reply = f"**Log Analysis: {service}** (last {tr})\nTotal entries: {result['total_entries']}\nErrors: {result['error_count']}\n\n{result['ai_summary']}"

        suggested = []
        if result['error_count'] > 0:
            summary_lower = result.get('ai_summary', '').lower()
            if "oom" in summary_lower or "memory" in summary_lower or "kill" in summary_lower:
                suggested = [
                    {"label": "Increase Memory", "command": "/ops scale-memory demo-app 512Mi", "risk": "medium"},
                    {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
                ]
            elif "connection" in summary_lower or "pool" in summary_lower or "timeout" in summary_lower:
                suggested = [
                    {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
                    {"label": "Scale Up", "command": "/ops scale demo-app 3", "risk": "medium"},
                ]
            else:
                suggested = [
                    {"label": "Diagnose", "command": f"/ops diagnose {service} errors found in logs", "risk": "low"},
                    {"label": "Restart Pod", "command": f"/ops restart {service}", "risk": "low"},
                ]

        return {"reply": reply, "actions_taken": ["log_analysis"], "sources": [], "suggested_actions": suggested}

    def _find_namespace(self, service: str) -> str:
        """Look up the namespace for a service from monitoring config."""
        from src.monitoring.config import get_monitoring_config
        config = get_monitoring_config()
        for svc in config.get_all():
            if svc.name == service:
                return svc.namespace
        return "demo-project"

    async def _cmd_restart(self, m) -> dict:
        service = m.group(1)
        namespace = self._find_namespace(service)
        from src.executor.engine import ExecutionEngine
        from src.monitoring.incidents import get_incident_store
        engine = ExecutionEngine()
        result = await engine.execute("restart_pod", service, namespace)
        store = get_incident_store()
        if result.get("success"):
            store.record("action", service, namespace, f"Restart pod: {result['message']}", actions=["restart_pod"])
            return {"reply": f"Executed: {result['message']}", "actions_taken": ["restart_pod"], "sources": []}
        return {"reply": result.get("message", "Failed"), "actions_taken": ["restart_pod"], "sources": []}

    async def _cmd_scale(self, m) -> dict:
        import json as json_lib
        service, replicas = m.group(1), int(m.group(2))
        namespace = self._find_namespace(service)
        from src.executor.engine import ExecutionEngine
        from src.monitoring.incidents import get_incident_store
        engine = ExecutionEngine()
        store = get_incident_store()
        # scale_down needs approval (medium risk), scale_up is also medium risk
        # Both use the same underlying logic — just set target replicas
        action = "scale_up" if replicas >= 2 else "scale_down"
        result = await engine.execute(action, service, namespace, json_lib.dumps({"replicas": replicas}))
        if result.get("needs_approval"):
            store.record("action", service, namespace, f"Scale to {replicas} replicas - PENDING APPROVAL", actions=["scale_pending"])
            return {"reply": f"APPROVAL REQUIRED\n\nI want to scale {service} to {replicas} replicas.\nThis is a medium-risk action.\n\nApproval ID: {result['approval_id']}", "actions_taken": ["scale_pending"], "sources": []}
        if result.get("success"):
            store.record("action", service, namespace, f"Scaled to {replicas}: {result['message']}", actions=["scale"])
            return {"reply": f"Executed: {result['message']}", "actions_taken": ["scale"], "sources": []}
        return {"reply": result.get("message", "Failed"), "actions_taken": ["scale"], "sources": []}

    async def _cmd_scale_memory(self, m) -> dict:
        import json as json_lib
        service, memory = m.group(1), m.group(2)
        namespace = self._find_namespace(service)
        from src.executor.engine import ExecutionEngine
        from src.monitoring.incidents import get_incident_store
        engine = ExecutionEngine()
        store = get_incident_store()
        result = await engine.execute("increase_memory", service, namespace, json_lib.dumps({"memory_limit": memory}))
        if result.get("needs_approval"):
            store.record("action", service, namespace, f"Increase memory to {memory} - PENDING APPROVAL", actions=["scale_memory_pending"])
            return {"reply": f"APPROVAL REQUIRED\n\nI want to increase {service} memory to {memory}.\nThis is a medium-risk action.\n\nApproval ID: {result['approval_id']}", "actions_taken": ["scale_memory_pending"], "sources": []}
        if result.get("success"):
            store.record("action", service, namespace, f"Memory increased to {memory}: {result['message']}", actions=["increase_memory"])
            return {"reply": f"Executed: {result['message']}", "actions_taken": ["increase_memory"], "sources": []}
        return {"reply": result.get("message", "Failed"), "actions_taken": ["increase_memory"], "sources": []}

    async def _cmd_rollback(self, m) -> dict:
        service = m.group(1)
        namespace = self._find_namespace(service)
        from src.executor.engine import ExecutionEngine
        from src.monitoring.incidents import get_incident_store
        engine = ExecutionEngine()
        store = get_incident_store()
        result = await engine.execute("rollback", service, namespace)
        if result.get("needs_approval"):
            store.record("action", service, namespace, f"Rollback - PENDING APPROVAL", actions=["rollback_pending"])
            return {"reply": f"APPROVAL REQUIRED\n\nI want to rollback {service} to previous version.\nThis is a medium-risk action.\n\nApproval ID: {result['approval_id']}", "actions_taken": ["rollback_pending"], "sources": []}
        if result.get("success"):
            store.record("action", service, namespace, f"Rolled back: {result['message']}", actions=["rollback"])
            return {"reply": f"Executed: {result['message']}", "actions_taken": ["rollback"], "sources": []}
        return {"reply": result.get("message", "Failed"), "actions_taken": ["rollback"], "sources": []}

    async def _cmd_approve(self, m) -> dict:
        approval_id = m.group(1)
        from src.executor.engine import ExecutionEngine
        engine = ExecutionEngine()
        result = await engine.approve(approval_id, "web-user")
        if result.get("success"):
            return {"reply": f"Approved and executed: {result['message']}", "actions_taken": ["approve"], "sources": []}
        return {"reply": result.get("message", "Approval failed"), "actions_taken": ["approve"], "sources": []}

    async def _cmd_help(self, m) -> dict:
        return {"reply": HELP, "actions_taken": [], "sources": []}

    async def _natural_language(self, msg: str) -> dict:
        result = await self.retriever.query(msg)
        return {"reply": result["answer"], "actions_taken": ["natural_language_query"], "sources": [s["source"] for s in result.get("sources",[])]}
