"""执行引擎 - 通过 Kubernetes API 执行真实运维操作"""
import uuid, json, os
from datetime import datetime, timezone
from kubernetes import client
from kubernetes.client import Configuration, ApiClient
from kubernetes.client.rest import ApiException

ACTIONS = {
    "restart_pod": {"risk": "low", "description": "Delete pod to trigger restart", "needs_approval": False},
    "scale_up": {"risk": "low", "description": "Increase deployment replicas", "needs_approval": False},
    "scale_down": {"risk": "medium", "description": "Decrease deployment replicas", "needs_approval": True},
    "rollback": {"risk": "medium", "description": "Rollback to previous revision", "needs_approval": True},
    "increase_memory": {"risk": "medium", "description": "Increase memory limits", "needs_approval": True},
}

_pending: dict[str, dict] = {}
_audit_log: list[dict] = []

TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

def _get_k8s_clients():
    """手动读取 ServiceAccount token 构建客户端（绕过 load_incluster_config 的 bug）"""
    conf = Configuration()
    conf.host = "https://kubernetes.default.svc"
    conf.ssl_ca_cert = CA_PATH
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            token = f.read().strip()
        conf.api_key = {"authorization": f"Bearer {token}"}
    api = ApiClient(conf)
    return client.CoreV1Api(api), client.AppsV1Api(api)

class ExecutionEngine:
    def __init__(self):
        self.core_api, self.apps_api = _get_k8s_clients()

    async def execute(self, action: str, service: str, namespace: str = "demo-project", parameters: str = "{}", approved_by: str = None) -> dict:
        if action not in ACTIONS:
            return {"success": False, "message": f"Unknown action: {action}. Available: {list(ACTIONS.keys())}"}

        defn = ACTIONS[action]
        params = json.loads(parameters) if isinstance(parameters, str) else parameters

        if defn["needs_approval"] and not approved_by:
            aid = str(uuid.uuid4())
            _pending[aid] = {"action": action, "service": service, "namespace": namespace, "parameters": parameters}
            return {"success": False, "needs_approval": True, "approval_id": aid, "message": f"APPROVAL REQUIRED - {action} ({defn['description']}), Risk: {defn['risk']}"}

        try:
            handler = getattr(self, f"_do_{action}", None)
            if not handler:
                return {"success": False, "message": f"No handler for {action}"}
            result = await handler(service, namespace, params)
            _audit_log.append({"action": action, "service": service, "namespace": namespace, "result": result, "time": datetime.now(timezone.utc).isoformat(), "approved_by": approved_by})
            return {"success": True, "message": result}
        except ApiException as e:
            return {"success": False, "message": f"K8s API error: {e.status} {e.reason}"}
        except Exception as e:
            return {"success": False, "message": f"Failed: {str(e)}"}

    async def approve(self, approval_id: str, by: str) -> dict:
        if approval_id not in _pending:
            return {"success": False, "message": "Approval not found"}
        p = _pending.pop(approval_id)
        return await self.execute(p["action"], p["service"], p["namespace"], p["parameters"], approved_by=by)

    async def _do_restart_pod(self, service: str, namespace: str, params: dict) -> str:
        pods = self.core_api.list_namespaced_pod(namespace=namespace, label_selector=f"app={service}")
        if not pods.items:
            return f"No pods found for app={service}"
        pod = pods.items[0]
        self.core_api.delete_namespaced_pod(name=pod.metadata.name, namespace=namespace)
        return f"Deleted pod {pod.metadata.name} - will be recreated by Deployment"

    async def _do_scale_up(self, service: str, namespace: str, params: dict) -> str:
        current = self.apps_api.read_namespaced_deployment(name=service, namespace=namespace)
        old = current.spec.replicas or 1
        new = params.get("replicas", old + 1)
        self.apps_api.patch_namespaced_deployment_scale(name=service, namespace=namespace, body={"spec": {"replicas": new}})
        return f"Scaled {service} from {old} to {new} replicas"

    async def _do_scale_down(self, service: str, namespace: str, params: dict) -> str:
        current = self.apps_api.read_namespaced_deployment(name=service, namespace=namespace)
        old = current.spec.replicas or 1
        new = max(1, params.get("replicas", old - 1))
        self.apps_api.patch_namespaced_deployment_scale(name=service, namespace=namespace, body={"spec": {"replicas": new}})
        return f"Scaled {service} from {old} to {new} replicas"

    async def _do_rollback(self, service: str, namespace: str, params: dict) -> str:
        return f"Rollback requested for {service} - requires manual verification"

    async def _do_increase_memory(self, service: str, namespace: str, params: dict) -> str:
        new_limit = params.get("memory_limit", "512Mi")
        deploy = self.apps_api.read_namespaced_deployment(name=service, namespace=namespace)
        deploy.spec.template.spec.containers[0].resources.limits["memory"] = new_limit
        self.apps_api.patch_namespaced_deployment(name=service, namespace=namespace, body=deploy)
        return f"Increased memory limit to {new_limit} for {service}"

    def get_pending(self) -> list[dict]:
        return [{"approval_id": k, **v} for k, v in _pending.items()]

    def get_audit_log(self) -> list[dict]:
        return _audit_log[-20:]
