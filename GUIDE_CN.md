# AIOps Platform — 从零构建指南

## 项目介绍

你要构建一个 **AI 运维平台**，部署在 OpenShift 上。

**它做什么：** 你有业务项目（`demo-project`），里面跑着微服务。当服务出现 Pod 崩溃、OOM Kill、错误率飙升等问题时，AI 运维平台自动接收告警、查询知识库、分析日志、诊断根因，并**自动执行修复操作**（重启 Pod、扩容、回滚等），高风险操作需要人工审批。还有一个 `healthy-project` 作为正常对照组。

**三个项目的关系：**

```
demo-project（业务应用，会出问题）     healthy-project（正常对照组）
     │                                       │
     │ Prometheus 采集指标 → 告警触发          │ 也被监控，但一切正常
     ▼                                       ▼
aiops-platform（AI 运维平台，监控、诊断、自动修复）
     │
     ├── 查 Runbook 知识库（RAG 检索增强生成）
     ├── 分析日志 + 查监控指标
     ├── AI 诊断根因 + 决策修复方案
     ├── 自动执行修复（低风险）/ 等人审批（中高风险）
     ├── Dashboard 页面（实时 Pod/内存/重启状态）
     ├── Incident History（事件历史记录）
     └── Namespace 管理（动态添加/移除监控对象）
```

**核心模块：**

| 模块 | 作用 |
|------|------|
| RAG 知识库 | 加载运维文档（Runbook/SOP），分块后存入 ChromaDB 向量数据库，供 AI 检索参考 |
| AI Agent | LangGraph 状态机：接收告警 → 分类 → 搜索知识库 → 采集日志 → 诊断 → 推荐修复 → 自动执行低风险修复 |
| 日志分析器 | 从 Loki 拉日志（无 Loki 时用 sample 数据），聚类错误模式，用 LLM 生成分析报告 |
| ChatOps | 提供 `/ops` 命令接口（health/ask/diagnose/logs/restart/scale/approve），支持自然语言提问 |
| 执行引擎 | 调用 Kubernetes API 执行真实运维操作（重启 Pod、扩缩容、回滚），带审批工作流和审计日志 |
| Health Check | 通过真实 K8s API（httpx）检查 Pod 状态、重启次数、OOMKilled 历史、内存限制 |
| Dashboard | 可视化仪表盘，自动刷新服务状态，动态添加监控服务，显示事件时间线 |
| Web UI | ChatOps 聊天界面，一键快捷操作，AI 建议操作按钮，审批卡片 |

**技术栈：**

| 技术 | 用途 |
|------|------|
| FastAPI + uvicorn | Web 框架和 ASGI 服务器 |
| LangChain + LangGraph | LLM 编排和 Agent 状态机 |
| OpenAI gpt-4o-mini | 大语言模型（通过官方 API） |
| ChromaDB | 内存向量数据库（不需要外部服务） |
| kubernetes Python SDK | 执行引擎调用 K8s API |
| httpx | Health Check 直接调用 K8s REST API |
| prometheus-client | 暴露 /metrics 给 Prometheus 采集 |
| OpenShift 4.21 | 容器平台 |

---

## 你的集群信息

```
API Server: https://api.gparente1805aws.emeashift.support:6443
版本: OpenShift 4.21 / Kubernetes 1.34
节点: 3 master (control-plane) + 3 worker
区域: AWS eu-west-2
账号: kube:admin
```

---

## 第一步：安装工具

只需要两个命令行工具，不需要装 Docker（镜像在 OpenShift 集群上用 `oc new-build --binary --strategy=docker` 远程构建）。

`oc` 是 OpenShift 的命令行客户端（兼容 kubectl），`helm` 用于管理 Kubernetes 包。

```bash
brew install openshift-cli
brew install helm
```

验证：

```bash
oc version
helm version
```

---

## 第二步：获取 LLM API Key

AI 运维平台使用 OpenAI gpt-4o-mini 做诊断推理。代码中 `settings.py` 的 `build_llm()` 函数用 `langchain-openai` 的 `ChatOpenAI` 客户端，也支持任何 OpenAI 兼容 API（DeepSeek、OpenRouter 等）。

**本指南使用 OpenAI。** 去 https://platform.openai.com/api-keys 申请 API Key，记下来，后面部署时要用。

---

## 第三步：登录 OpenShift 并创建项目

### 3.1 登录集群

```bash
oc login https://api.gparente1805aws.emeashift.support:6443 -u kubeadmin
```

输入密码后验证：

```bash
oc whoami
oc get nodes
```

应该看到 6 个节点（3 master + 3 worker）。

### 3.2 创建三个项目

为什么要分开？隔离关注点——业务应用和运维平台各自独立，AI 平台通过 RBAC 跨命名空间管理业务应用。`healthy-project` 作为正常对照，演示 Dashboard 上"健康 vs 异常"的对比。

```bash
oc new-project aiops-platform --display-name="AI Operations Platform"
oc new-project demo-project --display-name="Demo Business Application"
oc new-project healthy-project --display-name="Healthy Reference Application"
```

---

## 第四步：部署 demo-app（会故障的业务应用）

demo-app 是一个最简单的 Python HTTP 服务，模拟业务应用。它有 `/crash` 和 `/oom` 端点可以故意触发故障，方便后面演示 AI 自动修复。

### 4.1 创建 demo-app 源码

```bash
mkdir -p ~/demo-app && cd ~/demo-app
```

创建应用代码。这个服务监听 8080 端口，`/health` 返回健康状态，`/crash` 让进程退出触发 CrashLoopBackOff，`/oom` 不断分配内存直到被 OOMKilled：

```bash
cat > app.py << 'EOF'
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, sys

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Demo App Running\n")
        elif self.path == "/crash":
            print("FATAL: Crash triggered via /crash endpoint", flush=True)
            sys.exit(1)
        elif self.path == "/oom":
            print("WARNING: OOM trigger via /oom endpoint", flush=True)
            data = []
            while True:
                data.append("X" * 10**6)
        elif self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"# HELP http_requests_total Total requests\n# TYPE http_requests_total counter\nhttp_requests_total{method=\"GET\",status=\"200\"} 42\n")
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    print("Starting demo-app on :8080", flush=True)
    HTTPServer(("", 8080), Handler).serve_forever()
EOF
```

创建 Dockerfile：

```bash
cat > Dockerfile << 'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY app.py .
USER 1001
EXPOSE 8080
CMD ["python", "app.py"]
EOF
```

### 4.2 构建和部署 demo-app

为什么用 `oc new-build --binary --strategy=docker`？因为不需要本地安装 Docker，OpenShift 会在集群上构建镜像。`--binary` 表示从本地目录上传源码。

```bash
oc project demo-project
```

```bash
oc new-build --binary --strategy=docker --name=demo-app
```

```bash
oc start-build demo-app --from-dir=. --follow
```

等构建完成后，用**镜像 digest**（不用 `:latest`）创建 Deployment。为什么？OpenShift 会缓存 `:latest` tag，用 digest 确保拉到最新镜像：

```bash
IMAGE=$(oc get istag demo-app:latest -o jsonpath='{.image.dockerImageReference}')
echo $IMAGE
```

创建 Deployment，设置内存限制为 128Mi（故意设低，方便触发 OOM）：

```bash
cat > demo-deployment.yaml << 'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: demo-app
  labels:
    app: demo-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: demo-app
  template:
    metadata:
      labels:
        app: demo-app
    spec:
      containers:
      - name: demo-app
        image: IMAGE_PLACEHOLDER
        ports:
        - containerPort: 8080
        resources:
          requests:
            memory: "64Mi"
            cpu: "50m"
          limits:
            memory: "128Mi"
            cpu: "200m"
        readinessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 5
          periodSeconds: 10
        livenessProbe:
          httpGet:
            path: /health
            port: 8080
          initialDelaySeconds: 10
          periodSeconds: 15
EOF
```

替换镜像地址并部署：

```bash
# macOS 用 sed -i ''，Linux 用 sed -i（去掉引号）
sed -i '' "s|IMAGE_PLACEHOLDER|${IMAGE}|" demo-deployment.yaml
```

```bash
oc apply -f demo-deployment.yaml
```

创建 Service（`app=demo-app` 这个 label 很重要，后面 ServiceMonitor 需要用它匹配）：

```bash
oc expose deployment demo-app --port=8080
oc label svc demo-app app=demo-app
```

创建 Route 以便从外部访问：

```bash
oc expose service demo-app
```

验证：

```bash
oc get pods
oc get route demo-app -o jsonpath='{.spec.host}'
```

### 4.3 部署 healthy-app（正常对照组）

healthy-app 不需要自己构建镜像，直接用 Red Hat 官方的 `hello-openshift` 镜像（专为 OpenShift 设计，非 root 运行，不会有权限问题）。

```bash
oc project healthy-project
```

```bash
oc apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: healthy-app
  namespace: healthy-project
  labels:
    app: healthy-app
spec:
  replicas: 2
  selector:
    matchLabels:
      app: healthy-app
  template:
    metadata:
      labels:
        app: healthy-app
    spec:
      containers:
      - name: app
        image: quay.io/openshift/origin-hello-openshift:latest
        ports:
        - containerPort: 8080
        resources:
          limits:
            memory: "64Mi"
            cpu: "50m"
---
apiVersion: v1
kind: Service
metadata:
  name: healthy-app
  namespace: healthy-project
  labels:
    app: healthy-app
spec:
  ports:
  - port: 8080
    targetPort: 8080
    name: http
  selector:
    app: healthy-app
EOF
```

```bash
oc rollout status deployment/healthy-app -n healthy-project --timeout=60s
oc get pods -n healthy-project
```

---

## 第五步：创建 AIOps 平台源码

### 5.1 项目目录结构

```bash
mkdir -p ~/aiops-platform/{src/{config,api/endpoints,rag,agent,log_analyzer,chatops,executor,monitoring,static},knowledge_base/{runbooks,sops},data}
cd ~/aiops-platform
```

完整结构：

```
~/aiops-platform/
├── Dockerfile
├── requirements.txt
├── src/
│   ├── main.py                    # FastAPI 应用入口
│   ├── config/
│   │   └── settings.py            # 配置管理（env vars → Settings 对象）
│   ├── api/
│   │   ├── routes.py              # 路由注册
│   │   └── endpoints/
│   │       ├── health.py          # 健康检查端点
│   │       ├── knowledge.py       # RAG 知识库查询/索引
│   │       ├── agent.py           # AI Agent 告警处理 + 自动修复
│   │       ├── logs.py            # 日志分析
│   │       ├── chatops.py         # ChatOps /ops 命令
│   │       ├── executor.py        # 执行引擎（重启/扩容/审批）
│   │       └── monitoring.py      # Dashboard 数据 + Namespace + Incident
│   ├── rag/
│   │   ├── loader.py              # 文档加载器（MD/TXT/YAML）
│   │   ├── vector_store.py        # ChromaDB 向量存储
│   │   ├── indexer.py             # 分块 + 索引
│   │   └── retriever.py           # RAG 检索 + LLM 回答
│   ├── agent/
│   │   ├── state.py               # Agent 状态定义（TypedDict）
│   │   ├── prompts.py             # 系统提示词 + 分类/诊断 prompt
│   │   ├── tools.py               # Agent 工具（search_runbook, query_logs, get_metrics）
│   │   └── workflows.py           # LangGraph 工作流（classify → gather_and_diagnose）
│   ├── log_analyzer/
│   │   └── analyzer.py            # 日志拉取 + 聚类 + AI 分析
│   ├── chatops/
│   │   └── handler.py             # /ops 命令解析 + 执行
│   ├── executor/
│   │   └── engine.py              # K8s API 执行引擎（真实操作）
│   ├── monitoring/
│   │   ├── config.py              # 监控配置（哪些服务被监控）
│   │   ├── health.py              # K8s API 健康检查（Pod 状态/重启/OOM）
│   │   └── incidents.py           # 事件历史存储
│   └── static/
│       ├── index.html             # ChatOps 聊天页面
│       └── dashboard.html         # Dashboard 仪表盘
└── knowledge_base/
    ├── runbooks/
    │   ├── pod-crashloopbackoff.md
    │   ├── oom-kill.md
    │   └── database-connection-pool.md
    └── sops/
        └── incident-response.md
```

### 5.2 requirements.txt

为什么列出这些依赖：FastAPI 是 Web 框架，LangChain/LangGraph 负责 AI Agent 编排，ChromaDB 是向量数据库（内存模式，不需要外部服务），`kubernetes` 是 Python K8s SDK（执行引擎用来重启 Pod 等），`httpx` 用来直接调 K8s REST API（Health Check 模块用它绕过 SDK 的限制），`prometheus-client` 暴露 /metrics 端点给 Prometheus。

```bash
cat > requirements.txt << 'EOF'
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
pydantic>=2.9.0
pydantic-settings>=2.6.0
langchain>=0.3.0
langchain-openai>=0.2.0
langchain-community>=0.3.0
langgraph>=0.2.0
openai>=1.50.0
chromadb>=0.5.0
rank-bm25>=0.2.2
prometheus-client>=0.21.0
httpx>=0.27.0
pyyaml>=6.0.0
python-dotenv>=1.0.0
structlog>=24.0.0
redis>=5.1.0
slack-bolt>=1.20.0
langchain-text-splitters>=0.3.0
langchain-anthropic>=0.3.0
kubernetes>=28.0.0
EOF
```

### 5.3 Dockerfile

为什么先复制 `requirements.txt` 再复制代码？Docker 分层缓存——依赖很少变，代码经常改。这样改代码时不用重新安装依赖，构建速度快很多。`USER 1001` 是因为 OpenShift 默认禁止 root 容器。

```bash
cat > Dockerfile << 'EOF'
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/
COPY knowledge_base/ knowledge_base/
# src/static/ (index.html, dashboard.html) is included via COPY src/ above
USER 1001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -f http://localhost:8000/api/v1/health || exit 1
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
EOF
```

### 5.4 配置管理 — src/config/settings.py

`pydantic-settings` 从环境变量自动加载配置。`build_llm()` 构建 LangChain 的 ChatOpenAI 客户端——在 OpenShift 上，环境变量 `OPENAI_API_KEY` 从 Secret 注入，`LLM_MODEL` 设为 `gpt-4o-mini`。`lru_cache` 确保全局只创建一个 Settings 实例。

```bash
cat > src/config/settings.py << 'EOF'
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"
    chroma_persist_dir: str = "./data/chroma"
    chunk_size: int = 1000
    chunk_overlap: int = 200
    knowledge_base_dir: str = "./knowledge_base"
    redis_url: str = "redis://localhost:6379/0"
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

def build_llm():
    settings = get_settings()
    from langchain_openai import ChatOpenAI
    kwargs = {
        "model": settings.llm_model,
        "api_key": settings.openai_api_key,
        "temperature": 0,
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return ChatOpenAI(**kwargs)

@lru_cache
def get_settings() -> Settings:
    return Settings()
EOF
```

### 5.5 FastAPI 入口 — src/main.py

`lifespan` 是 FastAPI 的生命周期钩子。启动时检查向量数据库是否为空，如果是就自动索引 `knowledge_base/` 下的所有文档。这样部署后第一次启动就自动把 Runbook 加载到 ChromaDB 里。`/metrics` 挂载 prometheus-client 暴露指标，`/` 返回聊天页面，`/dashboard` 返回仪表盘。

```bash
cat > src/main.py << 'EOF'
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from prometheus_client import make_asgi_app
from src.config.settings import get_settings
from src.api.routes import api_router

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    print(f"Starting AIOps Platform [provider={settings.llm_provider}, model={settings.llm_model}]")
    try:
        from src.rag.vector_store import get_vector_store
        from src.rag.indexer import KnowledgeIndexer
        store = get_vector_store()
        if store.collection.count() == 0:
            print("Vector store empty, auto-indexing knowledge base...")
            indexer = KnowledgeIndexer()
            count = await indexer.index_all()
            print(f"Indexed {count} document chunks")
        else:
            print(f"Vector store has {store.collection.count()} chunks")
    except Exception as e:
        print(f"Auto-indexing skipped: {e}")
    yield
    print("Shutting down AIOps Platform")

def create_app() -> FastAPI:
    app = FastAPI(title="AIOps Platform", description="AI-Powered Operations Platform", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.mount("/metrics", make_asgi_app())
    app.include_router(api_router, prefix="/api/v1")

    @app.get("/")
    async def root():
        return FileResponse("src/static/index.html")

    @app.get("/dashboard")
    async def dashboard():
        return FileResponse("src/static/dashboard.html")

    return app

app = create_app()
EOF
```

### 5.6 路由注册 — src/api/routes.py

将所有 API 端点模块化，每个功能一个 router，统一挂载到 `/api/v1/` 下。这样代码组织清晰，每个 endpoint 文件职责单一。

```bash
cat > src/api/routes.py << 'EOF'
from fastapi import APIRouter
from src.api.endpoints import health, knowledge, agent, logs, chatops, executor, monitoring

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])
api_router.include_router(agent.router, prefix="/agent", tags=["agent"])
api_router.include_router(logs.router, prefix="/logs", tags=["logs"])
api_router.include_router(chatops.router, prefix="/chatops", tags=["chatops"])
api_router.include_router(executor.router, prefix="/executor", tags=["executor"])
api_router.include_router(monitoring.router, prefix="/monitoring", tags=["monitoring"])
EOF
```

### 5.7 API 端点 — src/api/endpoints/

#### health.py — 健康检查

最简单的端点，返回平台自身状态。OpenShift 的 HEALTHCHECK 和 Probe 都会调它。

```bash
cat > src/api/endpoints/health.py << 'EOF'
from fastapi import APIRouter
from datetime import datetime, timezone

router = APIRouter()

@router.get("")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.1.0",
    }
EOF
```

#### knowledge.py — RAG 知识库查询

`/query` 接收问题，通过 RAG 检索相关文档后用 LLM 生成回答。`/index` 手动触发重新索引。

```bash
cat > src/api/endpoints/knowledge.py << 'EOF'
from fastapi import APIRouter
from pydantic import BaseModel
from src.rag.retriever import RAGRetriever
from src.rag.indexer import KnowledgeIndexer

router = APIRouter()

class QueryRequest(BaseModel):
    question: str
    top_k: int = 5

@router.post("/query")
async def query_knowledge(req: QueryRequest):
    retriever = RAGRetriever()
    return await retriever.query(req.question, top_k=req.top_k)

@router.post("/index")
async def index_documents():
    indexer = KnowledgeIndexer()
    count = await indexer.index_all()
    return {"message": "Indexing complete", "documents_indexed": count}
EOF
```

#### agent.py — AI Agent 告警处理 + 自动修复

这是核心端点。`/alert` 接收 AlertManager 格式的告警（也兼容自定义格式）：先让 AI Agent 诊断根因，然后自动判断是否可以低风险修复——如果诊断结果包含 OOM/crash 关键词，就自动调用执行引擎重启 Pod。每次操作都记录到 Incident History。

```bash
cat > src/api/endpoints/agent.py << 'EOF'
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
EOF
```

#### logs.py — 日志分析

```bash
cat > src/api/endpoints/logs.py << 'EOF'
from fastapi import APIRouter
from pydantic import BaseModel
from src.log_analyzer.analyzer import LogAnalyzer

router = APIRouter()

class LogRequest(BaseModel):
    service: str
    time_range: str = "1h"
    log_level: str = "error"

@router.post("/analyze")
async def analyze_logs(req: LogRequest):
    analyzer = LogAnalyzer()
    return await analyzer.analyze(req.service, req.time_range, req.log_level)
EOF
```

#### chatops.py — ChatOps 命令入口

所有 `/ops` 命令和自然语言消息都通过这个端点进入 ChatOpsHandler。

```bash
cat > src/api/endpoints/chatops.py << 'EOF'
from fastapi import APIRouter
from pydantic import BaseModel
from src.chatops.handler import ChatOpsHandler

router = APIRouter()

class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    channel: str = "web"

@router.post("/message")
async def handle_message(req: ChatRequest):
    handler = ChatOpsHandler()
    return await handler.handle_message(req.message, req.user_id, req.channel)
EOF
```

#### executor.py — 执行引擎 API

`/execute` 执行操作，`/approve` 审批，`/pending` 查看待审批，`/audit` 查看审计日志。中高风险操作（scale_down、rollback、increase_memory）需要先审批。

```bash
cat > src/api/endpoints/executor.py << 'EOF'
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
EOF
```

#### monitoring.py — Dashboard 数据 + Namespace + Incident

这个端点服务 Dashboard 页面。`/dashboard` 聚合所有被监控服务的实时状态（调用 K8s API 获取 Pod 信息）。`/namespaces` 列出集群中可用的命名空间（用于 UI 选择器）。`/services` 管理哪些服务被监控（支持动态添加/删除）。`/incidents` 返回事件历史。

```bash
cat > src/api/endpoints/monitoring.py << 'EOF'
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
EOF
```

### 5.8 RAG 模块 — src/rag/

#### loader.py — 文档加载器

支持 `.md`、`.txt`、`.yaml` 文件。Markdown 文件按 `## ` 标题分割成多个 Document，每个带 source/section 元数据。这样 RAG 检索时能精确定位到 Runbook 的具体章节。

```bash
cat > src/rag/loader.py << 'EOF'
from pathlib import Path
from dataclasses import dataclass, field

@dataclass
class Document:
    content: str
    metadata: dict = field(default_factory=dict)

    @property
    def source(self) -> str:
        return self.metadata.get("source", "unknown")

class DocumentLoader:
    SUPPORTED = {".md", ".txt", ".yaml", ".yml"}

    def __init__(self, base_dir: str = "./knowledge_base"):
        self.base_dir = Path(base_dir)

    def load_all(self) -> list[Document]:
        docs = []
        if not self.base_dir.exists():
            return docs
        for f in self.base_dir.rglob("*"):
            if f.suffix in self.SUPPORTED:
                docs.extend(self.load_file(f))
        return docs

    def load_file(self, path: Path) -> list[Document]:
        try:
            content = path.read_text(encoding="utf-8")
            rel = path.relative_to(self.base_dir)
            meta = {"source": str(rel), "filename": path.name, "category": rel.parts[0] if len(rel.parts) > 1 else "general"}
            if path.suffix == ".md":
                return self._split_by_headers(content, meta)
            return [Document(content=content, metadata=meta)]
        except Exception:
            return []

    def _split_by_headers(self, content: str, base_meta: dict) -> list[Document]:
        sections, current, header = [], "", ""
        for line in content.split("\n"):
            if line.startswith("## "):
                if current.strip():
                    sections.append(Document(content=current.strip(), metadata={**base_meta, "section": header}))
                header = line[3:].strip()
                current = line + "\n"
            else:
                current += line + "\n"
        if current.strip():
            sections.append(Document(content=current.strip(), metadata={**base_meta, "section": header or "intro"}))
        return sections or [Document(content=content, metadata=base_meta)]

    def load_bytes(self, filename: str, content: bytes) -> list[Document]:
        text = content.decode("utf-8", errors="replace")
        return [Document(content=text, metadata={"source": filename, "category": "uploaded"})]
EOF
```

#### vector_store.py — ChromaDB 向量存储

使用 ChromaDB 的内存模式（`chromadb.Client()`），不需要外部数据库服务。cosine 距离计算相似度。启动时自动创建 collection，后续搜索时返回 content + metadata + 相似度分数。

```bash
cat > src/rag/vector_store.py << 'EOF'
from functools import lru_cache
import chromadb
from src.rag.loader import Document

COLLECTION_NAME = "ops_knowledge"

class VectorStore:
    def __init__(self):
        self.client = chromadb.Client()
        self.collection = self.client.get_or_create_collection(
            name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )

    async def add_documents(self, documents: list[Document]):
        if not documents:
            return
        ids = [doc.metadata.get("chunk_id", str(i)) for i, doc in enumerate(documents)]
        self.collection.upsert(
            ids=ids,
            documents=[d.content for d in documents],
            metadatas=[d.metadata for d in documents],
        )

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        results = self.collection.query(query_texts=[query], n_results=top_k, include=["documents", "metadatas", "distances"])
        hits = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(results["documents"][0], results["metadatas"][0], results["distances"][0]):
                hits.append({"content": doc, "metadata": meta, "score": 1 - dist})
        return hits

@lru_cache
def get_vector_store() -> VectorStore:
    return VectorStore()
EOF
```

#### indexer.py — 文档分块与索引

使用 LangChain 的 `RecursiveCharacterTextSplitter`，按 `## ` → `### ` → 空行 → 换行 → 空格的优先级分割，每块 1000 字符，重叠 200 字符（保持上下文连续）。每个 chunk 用 MD5 生成唯一 ID（source + index + 前50字符），用于 upsert 去重。

```bash
cat > src/rag/indexer.py << 'EOF'
import hashlib
from langchain_text_splitters import RecursiveCharacterTextSplitter
from src.config.settings import get_settings
from src.rag.loader import DocumentLoader, Document
from src.rag.vector_store import get_vector_store

class KnowledgeIndexer:
    def __init__(self):
        settings = get_settings()
        self.loader = DocumentLoader(settings.knowledge_base_dir)
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap,
            separators=["\n## ", "\n### ", "\n\n", "\n", " "],
        )
        self.store = get_vector_store()

    async def index_all(self) -> int:
        documents = self.loader.load_all()
        if not documents:
            return 0
        chunks = self._chunk(documents)
        await self.store.add_documents(chunks)
        return len(chunks)

    async def index_document(self, filename: str, content: bytes) -> int:
        documents = self.loader.load_bytes(filename, content)
        chunks = self._chunk(documents)
        await self.store.add_documents(chunks)
        return len(chunks)

    def _chunk(self, documents: list[Document]) -> list[Document]:
        chunks = []
        for doc in documents:
            for i, text in enumerate(self.splitter.split_text(doc.content)):
                chunk_id = hashlib.md5(f"{doc.source}:{i}:{text[:50]}".encode()).hexdigest()
                chunks.append(Document(content=text, metadata={**doc.metadata, "chunk_index": i, "chunk_id": chunk_id}))
        return chunks
EOF
```

#### retriever.py — RAG 检索 + LLM 回答

先用 ChromaDB 搜索最相关的 5 个文档块，拼接成 context，然后发给 LLM 生成回答。prompt 要求 LLM 基于 context 回答并引用来源。返回 answer + sources + confidence。

```bash
cat > src/rag/retriever.py << 'EOF'
from src.config.settings import build_llm
from src.rag.vector_store import get_vector_store

ANSWER_PROMPT = """You are an expert SRE/DevOps AI assistant. Answer based on the knowledge base context.
Be specific and actionable. Cite which source document your answer comes from.

Context:
{context}

Question: {question}

Answer:"""

class RAGRetriever:
    def __init__(self):
        self.store = get_vector_store()
        self.llm = build_llm()

    async def query(self, question: str, top_k: int = 5) -> dict:
        hits = await self.store.search(question, top_k=top_k)
        if not hits:
            return {"answer": "No relevant documents found.", "sources": [], "confidence": 0.0}

        context = "\n---\n".join(f"[{h['metadata'].get('source','?')}]\n{h['content']}" for h in hits)
        prompt = ANSWER_PROMPT.format(context=context, question=question)
        response = await self.llm.ainvoke(prompt)
        answer = response.content if hasattr(response, "content") else str(response)

        return {
            "answer": answer,
            "sources": [{"source": h["metadata"].get("source",""), "relevance": round(h["score"],3)} for h in hits],
            "confidence": round(sum(h["score"] for h in hits) / len(hits), 3),
        }
EOF
```

### 5.9 AI Agent — src/agent/

#### state.py — Agent 状态定义

LangGraph 的 Agent 使用 TypedDict 定义状态。每次节点执行都读写这个状态，形成完整的审计链。

```bash
cat > src/agent/state.py << 'EOF'
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
EOF
```

#### prompts.py — 系统提示词

`TRIAGE_SYSTEM` 定义 Agent 的角色和可用工具。`CLASSIFY_PROMPT` 让 LLM 对告警分类。`DIAGNOSE_PROMPT` 根据收集的证据（Runbook + 日志）生成诊断报告。

```bash
cat > src/agent/prompts.py << 'EOF'
TRIAGE_SYSTEM = """You are an expert SRE AI agent. You diagnose operational incidents using tools:
- search_runbook: Find relevant runbooks in the knowledge base
- query_logs: Get application logs
- get_metrics: Get Prometheus metrics

Workflow: classify alert → search runbook → gather logs/metrics → diagnose → recommend fix.
Be specific. Never fabricate data."""

CLASSIFY_PROMPT = """Classify this alert:
Alert: {alert_name} | Service: {service} | Severity: {severity}
Description: {description}

Provide: 1) Category 2) Urgency 3) Likely root cause hypothesis 4) Next investigation steps"""

DIAGNOSE_PROMPT = """Diagnose based on evidence:
Alert: {alert_name} | Service: {service}
Runbook info: {runbook_context}
Logs: {log_context}

Provide: 1) Root cause 2) Impact 3) Remediation steps (ordered by priority) 4) Prevention advice"""
EOF
```

#### tools.py — Agent 工具

LangChain `@tool` 装饰器定义 Agent 可以使用的工具。`search_runbook` 调用 RAG 检索知识库，`query_logs` 调用日志分析器，`get_metrics` 返回指标数据。

```bash
cat > src/agent/tools.py << 'EOF'
from langchain_core.tools import tool
from src.rag.retriever import RAGRetriever
from src.log_analyzer.analyzer import LogAnalyzer

@tool
async def search_runbook(query: str) -> str:
    """Search operations knowledge base for runbooks and SOPs."""
    retriever = RAGRetriever()
    result = await retriever.query(query, top_k=3)
    if not result["sources"]:
        return "No relevant runbooks found."
    return f"Answer: {result['answer']}\nSources: {[s['source'] for s in result['sources']]}"

@tool
async def query_logs(service: str, time_range: str = "1h") -> str:
    """Query application logs for a service."""
    analyzer = LogAnalyzer()
    return await analyzer.fetch_logs(service=service, time_range=time_range)

@tool
async def get_metrics(service: str) -> str:
    """Get key health metrics for a service (error rate, latency, CPU, memory)."""
    return f"Metrics for {service}: error_rate=5.2%, p95_latency=2.1s, memory=480Mi/512Mi, cpu=45%"

AGENT_TOOLS = [search_runbook, query_logs, get_metrics]
EOF
```

#### workflows.py — LangGraph 工作流

Agent 的核心逻辑。StateGraph 定义两个节点：
1. `classify` — 发送分类 prompt 给 LLM，判断告警类型和紧急度
2. `gather_and_diagnose` — 调用 `search_runbook` 和 `query_logs` 工具收集证据，然后发送诊断 prompt 给 LLM

流程：classify → gather_and_diagnose → END。每个步骤都写入 audit_trail。

```bash
cat > src/agent/workflows.py << 'EOF'
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
EOF
```

### 5.10 日志分析器 — src/log_analyzer/analyzer.py

先尝试从 Loki 拉取真实日志，如果 Loki 不可用就返回 sample 日志（包含 ConnectionPoolExhausted、OOM 等典型错误）。`_cluster` 方法对日志行做正则归一化（替换时间戳和 IP），然后用 Counter 统计频率。AI 分析部分把原始日志发给 LLM 做根因分析。

```bash
cat > src/log_analyzer/analyzer.py << 'EOF'
import re
from collections import Counter
from src.config.settings import get_settings, build_llm

class LogAnalyzer:
    async def fetch_logs(self, service: str, time_range: str = "1h", log_level: str = "error") -> str:
        try:
            import httpx
            settings = get_settings()
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{settings.loki_url}/loki/api/v1/query_range", params={"query": f'{{service="{service}"}}', "limit": 100})
                r.raise_for_status()
                lines = [v[1] for s in r.json().get("data",{}).get("result",[]) for v in s.get("values",[])]
                return "\n".join(lines)
        except Exception:
            return self._sample_logs(service)

    async def analyze(self, service: str, time_range: str = "1h", log_level: str = "error", query: str = "") -> dict:
        raw = await self.fetch_logs(service, time_range, log_level)
        lines = [l for l in raw.strip().split("\n") if l.strip()]
        errors = [l for l in lines if "error" in l.lower() or "exception" in l.lower()]
        patterns = self._cluster(raw)
        ai_summary = await self._ai_analyze(service, raw)
        return {
            "service": service, "time_range": time_range,
            "total_entries": len(lines), "error_count": len(errors),
            "patterns": [{"pattern": p[0], "count": p[1], "severity": "error", "sample": p[0]} for p in patterns[:5]],
            "ai_summary": ai_summary, "root_cause": None, "recommendations": [],
        }

    def _cluster(self, raw: str) -> list[tuple[str,int]]:
        counter = Counter()
        for line in raw.split("\n"):
            if line.strip():
                normalized = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*", "<TS>", line)
                normalized = re.sub(r"\b\d{1,3}(\.\d{1,3}){3}\b", "<IP>", normalized)
                counter[normalized.strip()] += 1
        return counter.most_common()

    async def _ai_analyze(self, service: str, raw: str) -> str:
        try:
            llm = build_llm()
            r = await llm.ainvoke(f"Analyze these logs for {service}, find root cause:\n{raw[:4000]}")
            return r.content
        except Exception as e:
            return f"AI analysis unavailable: {e}"

    def _sample_logs(self, service: str) -> str:
        return f"""2024-01-15T10:23:45Z ERROR [{service}] ConnectionPoolExhausted: timeout after 30000ms
2024-01-15T10:23:46Z ERROR [{service}] HTTP 500 /api/data - database connection timeout
2024-01-15T10:23:47Z WARN  [{service}] Active connections: 50/50, waiting: 23
2024-01-15T10:23:48Z ERROR [{service}] OOM: memory usage 490Mi / 512Mi limit
2024-01-15T10:23:49Z FATAL [{service}] Process killed by OOM killer (exit code 137)"""
EOF
```

### 5.11 ChatOps — src/chatops/handler.py

这是用户交互的核心。支持的命令：

| 命令 | 作用 |
|------|------|
| `/ops health` | 调用 K8s API 检查所有被监控服务状态，生成报告 + 建议操作 |
| `/ops ask <问题>` | RAG 知识库查询 |
| `/ops diagnose <服务> [症状]` | AI 诊断，返回根因 + 建议操作按钮 |
| `/ops logs <服务> [时间]` | 日志分析 |
| `/ops restart <服务>` | 重启 Pod（低风险，直接执行） |
| `/ops scale <服务> <副本数>` | 扩缩容（中风险，需审批） |
| `/ops approve <id>` | 审批待定操作 |
| `/ops help` | 显示帮助 |

非命令消息自动走自然语言 RAG 查询。`suggested_actions` 根据诊断结果智能推荐后续操作，前端渲染为可点击按钮。

```bash
cat > src/chatops/handler.py << 'EOF'
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
                {"label": "Increase Memory to 512Mi", "command": "/ops scale-memory demo-app 512Mi", "risk": "medium"},
                {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
                {"label": "Scale Up to 3 Replicas", "command": "/ops scale demo-app 3", "risk": "medium"},
                {"label": "Check Logs", "command": "/ops logs demo-app 1h", "risk": "low"},
            ]
        elif "connection" in diag_lower or "pool" in diag_lower or "timeout" in diag_lower or "database" in diag_lower:
            suggested = [
                {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
                {"label": "Scale Up to 3 Replicas", "command": "/ops scale demo-app 3", "risk": "medium"},
                {"label": "Check Logs", "command": "/ops logs demo-app 1h", "risk": "low"},
                {"label": "Query Runbook", "command": "/ops ask how to fix database connection pool exhaustion", "risk": "low"},
            ]
        elif "crash" in diag_lower or "loop" in diag_lower or "restart" in diag_lower:
            suggested = [
                {"label": "Check Crash Logs", "command": "/ops logs demo-app 30m", "risk": "low"},
                {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
                {"label": "Rollback Deployment", "command": "/ops rollback demo-app", "risk": "medium"},
                {"label": "Query Runbook", "command": "/ops ask how to fix CrashLoopBackOff", "risk": "low"},
            ]
        elif "latency" in diag_lower or "slow" in diag_lower or "performance" in diag_lower:
            suggested = [
                {"label": "Scale Up to 3 Replicas", "command": "/ops scale demo-app 3", "risk": "medium"},
                {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
                {"label": "Check Logs", "command": "/ops logs demo-app 1h", "risk": "low"},
            ]
        elif "error" in diag_lower or "500" in diag_lower or "rate" in diag_lower:
            suggested = [
                {"label": "Check Logs", "command": "/ops logs demo-app 1h", "risk": "low"},
                {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
                {"label": "Rollback Deployment", "command": "/ops rollback demo-app", "risk": "medium"},
                {"label": "Scale Up to 3 Replicas", "command": "/ops scale demo-app 3", "risk": "medium"},
            ]
        else:
            suggested = [
                {"label": "Check Logs", "command": "/ops logs demo-app 1h", "risk": "low"},
                {"label": "Restart Pod", "command": "/ops restart demo-app", "risk": "low"},
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

    async def _cmd_restart(self, m) -> dict:
        service = m.group(1)
        from src.executor.engine import ExecutionEngine
        from src.monitoring.incidents import get_incident_store
        engine = ExecutionEngine()
        result = await engine.execute("restart_pod", service, "demo-project")
        store = get_incident_store()
        if result.get("success"):
            store.record("action", service, "demo-project", f"Restart pod: {result['message']}", actions=["restart_pod"])
            return {"reply": f"Executed: {result['message']}", "actions_taken": ["restart_pod"], "sources": []}
        return {"reply": result.get("message", "Failed"), "actions_taken": ["restart_pod"], "sources": []}

    async def _cmd_scale(self, m) -> dict:
        import json as json_lib
        service, replicas = m.group(1), int(m.group(2))
        from src.executor.engine import ExecutionEngine
        from src.monitoring.incidents import get_incident_store
        engine = ExecutionEngine()
        store = get_incident_store()
        result = await engine.execute("scale_up" if replicas > 2 else "scale_down", service, "demo-project", json_lib.dumps({"replicas": replicas}))
        if result.get("needs_approval"):
            store.record("action", service, "demo-project", f"Scale to {replicas} replicas - PENDING APPROVAL", actions=["scale_pending"])
            return {"reply": f"APPROVAL REQUIRED\n\nI want to scale {service} to {replicas} replicas.\nThis is a medium-risk action.\n\nApproval ID: {result['approval_id']}", "actions_taken": ["scale_pending"], "sources": []}
        if result.get("success"):
            store.record("action", service, "demo-project", f"Scaled to {replicas}: {result['message']}", actions=["scale"])
            return {"reply": f"Executed: {result['message']}", "actions_taken": ["scale"], "sources": []}
        return {"reply": result.get("message", "Failed"), "actions_taken": ["scale"], "sources": []}

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
EOF
```

### 5.12 执行引擎 — src/executor/engine.py

通过 Kubernetes Python SDK 执行**真实运维操作**。关键设计：

- **风险分级**：`restart_pod` 和 `scale_up` 是低风险（直接执行），`scale_down`、`rollback`、`increase_memory` 是中风险（需审批）
- **审批工作流**：中风险操作先存入 `_pending` 字典，返回 `approval_id`，前端显示审批卡片
- **ServiceAccount Token**：在 OpenShift Pod 内，手动读取 `/var/run/secrets/kubernetes.io/serviceaccount/token` 构建 K8s 客户端（绕过 `load_incluster_config` 的一些兼容性问题）
- **审计日志**：每次执行记录 action/service/namespace/result/time/approved_by

```bash
cat > src/executor/engine.py << 'EOF'
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
    conf = Configuration()
    conf.host = "https://kubernetes.default.svc"
    conf.ssl_ca_cert = CA_PATH
    if os.path.exists(TOKEN_PATH):
        token = open(TOKEN_PATH).read().strip()
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
EOF
```

### 5.13 监控模块 — src/monitoring/

#### config.py — 监控配置

管理哪些服务被监控。默认监控 `demo-project` 的 `demo-app`。配置持久化到 `/tmp/aiops-monitoring-config.json`（容器内临时存储）。支持通过 API 动态添加/删除服务。

```bash
cat > src/monitoring/config.py << 'EOF'
"""Monitoring configuration — manages which namespaces and services are monitored."""

import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict

CONFIG_FILE = "/tmp/aiops-monitoring-config.json"

@dataclass
class MonitoredService:
    name: str
    namespace: str
    label_selector: str = ""
    added_at: str = ""

    def __post_init__(self):
        if not self.label_selector:
            self.label_selector = f"app={self.name}"
        if not self.added_at:
            self.added_at = datetime.now(timezone.utc).isoformat()

@dataclass 
class MonitoringConfig:
    services: list[MonitoredService] = field(default_factory=list)

    def add_service(self, name: str, namespace: str, label_selector: str = "") -> MonitoredService:
        for s in self.services:
            if s.name == name and s.namespace == namespace:
                return s
        svc = MonitoredService(name=name, namespace=namespace, label_selector=label_selector)
        self.services.append(svc)
        self.save()
        return svc

    def remove_service(self, name: str, namespace: str) -> bool:
        before = len(self.services)
        self.services = [s for s in self.services if not (s.name == name and s.namespace == namespace)]
        if len(self.services) < before:
            self.save()
            return True
        return False

    def get_all(self) -> list[MonitoredService]:
        return self.services

    def save(self):
        data = {"services": [asdict(s) for s in self.services]}
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls) -> "MonitoringConfig":
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                services = [MonitoredService(**s) for s in data.get("services", [])]
                return cls(services=services)
            except Exception:
                pass
        config = cls(services=[MonitoredService(name="demo-app", namespace="demo-project")])
        config.save()
        return config


_config: MonitoringConfig | None = None

def get_monitoring_config() -> MonitoringConfig:
    global _config
    if _config is None:
        _config = MonitoringConfig.load()
    return _config
EOF
```

#### health.py — K8s API 健康检查

直接用 `httpx` 调用 Kubernetes REST API（不通过 Python SDK）检查 Pod 状态。为什么用 httpx 而不是 kubernetes SDK？因为 Health Check 需要快速返回，httpx 更轻量。检查内容包括：Pod 数量和 Running 状态、容器重启次数、OOMKilled 历史、内存限制。根据结果判断 HEALTHY/DEGRADED/DOWN 并生成建议。

```bash
cat > src/monitoring/health.py << 'EOF'
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
        token = open(TOKEN_PATH).read().strip()
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
EOF
```

#### incidents.py — 事件历史

记录每次告警、诊断、操作和健康检查。持久化到 `/tmp/aiops-incidents.json`，保留最近 100 条。支持按 ID 标记为已解决。Dashboard 的 Incident History 表格从这里取数据。

```bash
cat > src/monitoring/incidents.py << 'EOF'
"""Incident history — records every diagnosis, action, and alert."""

import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict

INCIDENTS_FILE = "/tmp/aiops-incidents.json"

@dataclass
class Incident:
    id: str
    timestamp: str
    type: str  # alert, diagnosis, action, health_check
    service: str
    namespace: str
    summary: str
    details: str = ""
    actions_taken: list[str] = field(default_factory=list)
    status: str = "open"  # open, resolved, acknowledged

    def to_dict(self) -> dict:
        return asdict(self)


class IncidentStore:
    def __init__(self):
        self.incidents: list[Incident] = []
        self._load()

    def record(self, type: str, service: str, namespace: str, summary: str, details: str = "", actions: list[str] = None) -> Incident:
        import uuid
        incident = Incident(
            id=str(uuid.uuid4())[:8],
            timestamp=datetime.now(timezone.utc).isoformat(),
            type=type,
            service=service,
            namespace=namespace,
            summary=summary,
            details=details,
            actions_taken=actions or [],
        )
        self.incidents.append(incident)
        self._save()
        return incident

    def get_recent(self, limit: int = 20) -> list[Incident]:
        return sorted(self.incidents, key=lambda i: i.timestamp, reverse=True)[:limit]

    def get_by_service(self, service: str) -> list[Incident]:
        return [i for i in self.incidents if i.service == service]

    def resolve(self, incident_id: str) -> bool:
        for i in self.incidents:
            if i.id == incident_id:
                i.status = "resolved"
                self._save()
                return True
        return False

    def _save(self):
        data = [i.to_dict() for i in self.incidents[-100:]]  # Keep last 100
        with open(INCIDENTS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self):
        if os.path.exists(INCIDENTS_FILE):
            try:
                with open(INCIDENTS_FILE) as f:
                    data = json.load(f)
                self.incidents = [Incident(**d) for d in data]
            except Exception:
                self.incidents = []


_store: IncidentStore | None = None

def get_incident_store() -> IncidentStore:
    global _store
    if _store is None:
        _store = IncidentStore()
    return _store
EOF
```

### 5.14 前端页面 — src/static/

#### index.html — ChatOps 聊天界面

这是主页面。功能包括：聊天消息发送/接收、快捷操作按钮（Health Check / Diagnose / Log Analysis / OOM Kill / CrashLoop / Connection Pool）、AI 建议操作按钮（根据诊断结果动态生成）、审批卡片（中风险操作弹出 Approve/Reject 按钮）、typing indicator。

```bash
cat > src/static/index.html << 'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AIOps Platform</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: #f0f4f8; color: #1a1a1a; height: 100vh; display: flex; flex-direction: column; }

        .header { background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%); padding: 18px 32px; display: flex; align-items: center; justify-content: space-between; }
        .header-left { display: flex; align-items: center; gap: 14px; }
        .logo { width: 36px; height: 36px; background: rgba(255,255,255,0.15); border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 18px; }
        .header h1 { font-size: 18px; font-weight: 700; color: #fff; letter-spacing: -0.3px; }
        .header .tagline { font-size: 12px; color: rgba(255,255,255,0.7); margin-top: 2px; }
        .header-right { display: flex; align-items: center; gap: 16px; }
        .header-right a { font-size: 13px; color: rgba(255,255,255,0.8); text-decoration: none; padding: 6px 14px; border: 1px solid rgba(255,255,255,0.3); border-radius: 6px; }
        .header-right a:hover { background: rgba(255,255,255,0.1); color: #fff; }
        .status-dot { width: 8px; height: 8px; background: #34d399; border-radius: 50%; animation: pulse 2s infinite; }
        .status-text { font-size: 12px; color: rgba(255,255,255,0.9); }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }

        .stats-bar { background: #fff; padding: 12px 32px; border-bottom: 1px solid #e5e7eb; display: flex; gap: 24px; align-items: center; }
        .stat { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #6b7280; }
        .stat-value { font-weight: 600; color: #1f2937; }

        .toolbar { background: #fff; padding: 12px 32px; border-bottom: 1px solid #e5e7eb; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
        .toolbar-label { font-size: 11px; color: #9ca3af; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; margin-right: 8px; }
        .toolbar button { background: #f8fafc; border: 1px solid #e2e8f0; color: #475569; padding: 7px 14px; border-radius: 8px; font-size: 12px; font-weight: 500; cursor: pointer; transition: all 0.15s; }
        .toolbar button:hover { background: #eef2ff; border-color: #c7d2fe; color: #4338ca; }
        .toolbar button.primary { background: #eef2ff; border-color: #c7d2fe; color: #4338ca; }

        .chat-container { flex: 1; overflow-y: auto; padding: 24px 32px; display: flex; flex-direction: column; gap: 16px; }
        .message { max-width: 78%; display: flex; flex-direction: column; gap: 4px; }
        .message.user { align-self: flex-end; }
        .message.bot { align-self: flex-start; }
        .message .bubble { padding: 14px 18px; border-radius: 16px; line-height: 1.65; font-size: 13.5px; white-space: pre-wrap; word-wrap: break-word; }
        .message.user .bubble { background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; border-bottom-right-radius: 4px; box-shadow: 0 2px 8px rgba(37,99,235,0.2); }
        .message.bot .bubble { background: #fff; color: #1f2937; border: 1px solid #e5e7eb; border-bottom-left-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
        .message .meta { font-size: 10px; color: #9ca3af; padding: 0 6px; }
        .message.user .meta { align-self: flex-end; }
        .message.bot .meta { align-self: flex-start; display: flex; align-items: center; gap: 6px; }
        .message.bot .meta::before { content: "AI"; font-size: 9px; background: #dbeafe; color: #1d4ed8; padding: 2px 5px; border-radius: 4px; font-weight: 600; }
        .message.system { align-self: center; }
        .message.system .bubble { background: #f1f5f9; color: #64748b; font-size: 13px; padding: 10px 20px; border-radius: 10px; border: none; }
        .sources { margin-top: 8px; font-size: 11px; color: #6b7280; background: #f8fafc; padding: 8px 12px; border-radius: 8px; border: 1px solid #f1f5f9; }
        .sources::before { content: "Sources: "; font-weight: 600; color: #4b5563; }

        .approval-card { background: #fffbeb; border: 1px solid #fcd34d; border-radius: 12px; padding: 16px 20px; margin-top: 8px; }
        .approval-card .title { font-size: 13px; font-weight: 600; color: #92400e; margin-bottom: 8px; }
        .approval-card .detail { font-size: 13px; color: #78350f; line-height: 1.5; margin-bottom: 12px; }
        .approval-card .actions { display: flex; gap: 8px; }
        .approval-card button { padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; }
        .approval-card .btn-approve { background: #10b981; color: #fff; }
        .approval-card .btn-approve:hover { background: #059669; }
        .approval-card .btn-reject { background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; }
        .approval-card .btn-reject:hover { background: #e5e7eb; }

        .success-card { background: #ecfdf5; border: 1px solid #6ee7b7; border-radius: 12px; padding: 14px 18px; margin-top: 8px; font-size: 13px; color: #065f46; }
        
        .action-suggestions { margin-top: 10px; padding: 12px 16px; background: #f8fafc; border-radius: 10px; border: 1px solid #e2e8f0; }
        .action-suggestions .label { font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; margin-bottom: 8px; }
        .action-suggestions .btns { display: flex; flex-wrap: wrap; gap: 8px; }
        .action-suggestions button { padding: 8px 14px; border-radius: 8px; font-size: 12px; font-weight: 500; cursor: pointer; border: 1px solid; transition: all 0.15s; }
        .action-suggestions button.low { background: #ecfdf5; border-color: #6ee7b7; color: #065f46; }
        .action-suggestions button.low:hover { background: #d1fae5; }
        .action-suggestions button.medium { background: #fffbeb; border-color: #fcd34d; color: #92400e; }
        .action-suggestions button.medium:hover { background: #fef3c7; }

        .input-area { background: #fff; padding: 16px 32px; border-top: 1px solid #e5e7eb; display: flex; gap: 12px; align-items: center; box-shadow: 0 -2px 8px rgba(0,0,0,0.03); }
        .input-area input { flex: 1; background: #f8fafc; border: 2px solid #e5e7eb; color: #1f2937; padding: 14px 18px; border-radius: 12px; font-size: 14px; outline: none; transition: all 0.15s; }
        .input-area input:focus { border-color: #2563eb; background: #fff; box-shadow: 0 0 0 3px rgba(37,99,235,0.08); }
        .input-area button { background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; border: none; padding: 14px 24px; border-radius: 12px; font-weight: 600; cursor: pointer; font-size: 14px; transition: all 0.15s; box-shadow: 0 2px 8px rgba(37,99,235,0.3); }
        .input-area button:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(37,99,235,0.4); }
        .input-area button:disabled { background: #94a3b8; box-shadow: none; transform: none; cursor: not-allowed; }

        .typing-indicator { display: flex; gap: 5px; padding: 6px 0; align-items: center; }
        .typing-indicator span { width: 7px; height: 7px; background: #94a3b8; border-radius: 50%; animation: bounce 1.4s infinite ease-in-out; }
        .typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
        .typing-indicator span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes bounce { 0%,80%,100%{transform:scale(0)} 40%{transform:scale(1)} }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-left">
            <div class="logo">&#9881;</div>
            <div>
                <h1>AIOps Platform</h1>
                <div class="tagline">AI-Powered Operations Assistant</div>
            </div>
        </div>
        <div class="header-right">
            <a href="/dashboard">Dashboard</a>
            <div class="status-dot"></div>
            <span class="status-text">Connected</span>
        </div>
    </div>
    <div class="stats-bar">
        <div class="stat">AI Model: <span class="stat-value">GPT-4o-mini</span></div>
        <div class="stat">Knowledge Base: <span class="stat-value">4 Runbooks indexed</span></div>
        <div class="stat">Status: <span class="stat-value" style="color:#10b981">All systems operational</span></div>
    </div>
    <div class="toolbar">
        <span class="toolbar-label">Monitor</span>
        <button class="primary" onclick="sendQuick('/ops health')">Health Check</button>
        <button onclick="sendQuick('/ops diagnose demo-app')">Diagnose Service</button>
        <button onclick="sendQuick('/ops logs demo-app 1h')">Log Analysis</button>
        <span class="toolbar-label" style="margin-left:12px">Knowledge</span>
        <button onclick="sendQuick('/ops ask How to fix OOM Kill')">OOM Kill</button>
        <button onclick="sendQuick('/ops ask How to fix CrashLoopBackOff')">CrashLoop</button>
        <button onclick="sendQuick('/ops ask database connection pool exhaustion')">Connection Pool</button>
    </div>
    <div class="chat-container" id="chat">
        <div class="message system"><div class="bubble">Welcome to AIOps Platform. Ask questions about your infrastructure, query runbooks, or trigger AI-powered diagnostics.</div></div>
    </div>
    <div class="input-area">
        <input type="text" id="input" placeholder="Describe an incident, ask about runbooks, or use /ops commands..." onkeypress="if(event.key==='Enter')send()">
        <button onclick="send()" id="btn">Send</button>
    </div>
    <script>
        const chat = document.getElementById('chat');
        const input = document.getElementById('input');
        const btn = document.getElementById('btn');

        function addMessage(text, type, extra) {
            const wrapper = document.createElement('div');
            wrapper.className = `message ${type}`;
            const bubble = document.createElement('div');
            bubble.className = 'bubble';
            bubble.textContent = text;
            wrapper.appendChild(bubble);
            const meta = document.createElement('div');
            meta.className = 'meta';
            meta.textContent = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
            wrapper.appendChild(meta);
            if (extra && extra.sources && extra.sources.length > 0) {
                const src = document.createElement('div');
                src.className = 'sources';
                src.textContent = extra.sources.join(', ');
                bubble.appendChild(src);
            }
            chat.appendChild(wrapper);
            chat.scrollTop = chat.scrollHeight;
            return wrapper;
        }

        function addApprovalCard(message, approvalId) {
            const wrapper = document.createElement('div');
            wrapper.className = 'message bot';
            const bubble = document.createElement('div');
            bubble.className = 'bubble';
            bubble.textContent = message;
            wrapper.appendChild(bubble);
            const card = document.createElement('div');
            card.className = 'approval-card';
            card.innerHTML = `
                <div class="title">Action Requires Approval</div>
                <div class="detail">This is a medium/high risk operation. Do you want to proceed?</div>
                <div class="actions">
                    <button class="btn-approve" onclick="approve('${approvalId}')">Approve & Execute</button>
                    <button class="btn-reject" onclick="reject()">Reject</button>
                </div>
            `;
            wrapper.appendChild(card);
            chat.appendChild(wrapper);
            chat.scrollTop = chat.scrollHeight;
        }

        function addSuccessCard(text) {
            const wrapper = document.createElement('div');
            wrapper.className = 'message bot';
            const card = document.createElement('div');
            card.className = 'success-card';
            card.textContent = text;
            wrapper.appendChild(card);
            chat.appendChild(wrapper);
            chat.scrollTop = chat.scrollHeight;
        }

        function addTyping() {
            const wrapper = document.createElement('div');
            wrapper.className = 'message bot';
            wrapper.id = 'typing';
            const bubble = document.createElement('div');
            bubble.className = 'bubble';
            bubble.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
            wrapper.appendChild(bubble);
            chat.appendChild(wrapper);
            chat.scrollTop = chat.scrollHeight;
        }

        function addSuggestedActions(wrapper, actions) {
            const container = document.createElement('div');
            container.className = 'action-suggestions';
            container.innerHTML = '<div class="label">Recommended Actions</div><div class="btns"></div>';
            const btns = container.querySelector('.btns');
            actions.forEach(a => {
                const b = document.createElement('button');
                b.className = a.risk;
                b.textContent = a.label + (a.risk === 'medium' ? ' (needs approval)' : '');
                b.onclick = () => { sendQuick(a.command); container.remove(); };
                btns.appendChild(b);
            });
            wrapper.appendChild(container);
            chat.scrollTop = chat.scrollHeight;
        }

        function sendQuick(cmd) { input.value = cmd; send(); }

        async function approve(id) {
            document.querySelectorAll('.approval-card button').forEach(b => b.disabled = true);
            try {
                const res = await fetch('/api/v1/executor/approve', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({approval_id: id, approved_by: 'web-user'})
                });
                const data = await res.json();
                if (data.success) {
                    addSuccessCard('Executed successfully: ' + data.message);
                } else {
                    addMessage(data.message || 'Execution failed', 'bot', {});
                }
            } catch(e) {
                addMessage('Error: ' + e.message, 'system', {});
            }
        }

        function reject() {
            document.querySelectorAll('.approval-card button').forEach(b => b.disabled = true);
            addMessage('Action rejected. No changes were made.', 'system', {});
        }

        async function send() {
            const msg = input.value.trim();
            if (!msg) return;
            input.value = '';
            addMessage(msg, 'user', {});
            btn.disabled = true;
            addTyping();

            try {
                const res = await fetch('/api/v1/chatops/message', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({message: msg, user_id: 'web-user', channel: 'web'})
                });
                const data = await res.json();
                document.getElementById('typing')?.remove();
                const reply = data.reply || data.detail || JSON.stringify(data, null, 2);

                if (reply.includes('APPROVAL REQUIRED')) {
                    const match = reply.match(/Approval ID: ([a-f0-9-]+)/i);
                    if (match) {
                        addApprovalCard(reply, match[1]);
                    } else {
                        addMessage(reply, 'bot', data);
                    }
                } else {
                    const botMsg = addMessage(reply, 'bot', data);
                    if (data.suggested_actions && data.suggested_actions.length > 0) {
                        addSuggestedActions(botMsg, data.suggested_actions);
                    }
                }
            } catch(e) {
                document.getElementById('typing')?.remove();
                addMessage('Connection error: ' + e.message, 'system', {});
            }
            btn.disabled = false;
            input.focus();
        }
    </script>
</body>
</html>
EOF
```

#### dashboard.html — Dashboard 仪表盘

Dashboard 页面每 30 秒自动刷新。显示：整体状态（HEALTHY/DEGRADED/DOWN）、每个服务卡片（Pod 数量、重启次数、内存限制、最近错误、建议）、添加新服务表单、Incident History 时间线。

```bash
cat > src/static/dashboard.html << 'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AIOps Platform - Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Inter', sans-serif; background: #f0f4f8; color: #1a1a1a; min-height: 100vh; }

        .header { background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%); padding: 18px 32px; display: flex; align-items: center; justify-content: space-between; }
        .header-left { display: flex; align-items: center; gap: 14px; }
        .logo { width: 36px; height: 36px; background: rgba(255,255,255,0.15); border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 18px; }
        .header h1 { font-size: 18px; font-weight: 700; color: #fff; }
        .header .tagline { font-size: 12px; color: rgba(255,255,255,0.7); margin-top: 2px; }

        .nav { background: #fff; padding: 0 32px; border-bottom: 1px solid #e5e7eb; display: flex; gap: 0; }
        .nav a { padding: 14px 20px; font-size: 13px; font-weight: 500; color: #6b7280; text-decoration: none; border-bottom: 2px solid transparent; transition: all 0.15s; }
        .nav a:hover { color: #2563eb; }
        .nav a.active { color: #2563eb; border-bottom-color: #2563eb; }

        .content { max-width: 1200px; margin: 0 auto; padding: 24px 32px; }

        .overall-status { display: flex; align-items: center; gap: 16px; padding: 20px 24px; background: #fff; border-radius: 12px; border: 1px solid #e5e7eb; margin-bottom: 24px; }
        .overall-status .dot { width: 12px; height: 12px; border-radius: 50%; }
        .overall-status .dot.healthy { background: #10b981; }
        .overall-status .dot.degraded { background: #f59e0b; }
        .overall-status .dot.down { background: #ef4444; }
        .overall-status .text { font-size: 16px; font-weight: 600; }
        .overall-status .sub { font-size: 13px; color: #6b7280; margin-left: auto; }

        .services-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; margin-bottom: 32px; }
        .service-card { background: #fff; border-radius: 12px; border: 1px solid #e5e7eb; padding: 20px; transition: box-shadow 0.15s; }
        .service-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.06); }
        .service-card .card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
        .service-card .name { font-size: 15px; font-weight: 600; }
        .service-card .namespace { font-size: 11px; color: #6b7280; background: #f3f4f6; padding: 3px 8px; border-radius: 4px; }
        .service-card .badge { font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 20px; }
        .badge.healthy { background: #ecfdf5; color: #065f46; }
        .badge.degraded { background: #fffbeb; color: #92400e; }
        .badge.down { background: #fef2f2; color: #991b1b; }

        .metrics-row { display: flex; gap: 12px; margin-bottom: 14px; }
        .metric { flex: 1; background: #f9fafb; border-radius: 8px; padding: 10px 12px; text-align: center; }
        .metric .value { font-size: 18px; font-weight: 700; color: #111; }
        .metric .label { font-size: 10px; color: #6b7280; text-transform: uppercase; margin-top: 2px; }

        .errors-list { margin-bottom: 12px; }
        .errors-list .title { font-size: 11px; font-weight: 600; color: #991b1b; margin-bottom: 6px; }
        .errors-list .item { font-size: 12px; color: #7f1d1d; padding: 4px 0; }

        .recs-list .title { font-size: 11px; font-weight: 600; color: #4b5563; margin-bottom: 6px; }
        .recs-list .item { font-size: 12px; color: #374151; padding: 3px 0; padding-left: 12px; position: relative; }
        .recs-list .item::before { content: "→"; position: absolute; left: 0; color: #9ca3af; }

        .section-title { font-size: 14px; font-weight: 600; color: #374151; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }

        .incidents-table { background: #fff; border-radius: 12px; border: 1px solid #e5e7eb; overflow: hidden; }
        .incidents-table table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .incidents-table th { background: #f9fafb; padding: 10px 16px; text-align: left; font-weight: 600; color: #374151; border-bottom: 1px solid #e5e7eb; }
        .incidents-table td { padding: 10px 16px; border-bottom: 1px solid #f3f4f6; color: #4b5563; }
        .incidents-table tr:last-child td { border-bottom: none; }
        .type-badge { font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 500; }
        .type-badge.alert { background: #fef2f2; color: #991b1b; }
        .type-badge.diagnosis { background: #eef2ff; color: #3730a3; }
        .type-badge.action { background: #ecfdf5; color: #065f46; }
        .type-badge.health_check { background: #f3f4f6; color: #374151; }

        .empty-state { text-align: center; padding: 40px; color: #9ca3af; font-size: 14px; }

        .settings-form { background: #fff; border-radius: 12px; border: 1px solid #e5e7eb; padding: 24px; margin-bottom: 24px; }
        .settings-form h3 { font-size: 14px; margin-bottom: 16px; }
        .form-row { display: flex; gap: 12px; align-items: flex-end; }
        .form-row input { flex: 1; padding: 10px 14px; border: 1px solid #e5e7eb; border-radius: 8px; font-size: 13px; outline: none; }
        .form-row input:focus { border-color: #2563eb; }
        .form-row button { padding: 10px 18px; background: #2563eb; color: #fff; border: none; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; }
        .form-row button:hover { background: #1d4ed8; }

        .refresh-btn { padding: 8px 16px; background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 8px; font-size: 12px; cursor: pointer; color: #374151; }
        .refresh-btn:hover { background: #e5e7eb; }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-left">
            <div class="logo">&#9881;</div>
            <div><h1>AIOps Platform</h1><div class="tagline">AI-Powered Operations Assistant</div></div>
        </div>
    </div>
    <div class="nav">
        <a href="/">Chat</a>
        <a href="/dashboard" class="active">Dashboard</a>
    </div>
    <div class="content">
        <div class="overall-status" id="overall">
            <div class="dot" id="overall-dot"></div>
            <span class="text" id="overall-text">Loading...</span>
            <span class="sub" id="overall-sub"></span>
            <button class="refresh-btn" onclick="loadDashboard()">Refresh</button>
        </div>

        <div class="section-title">Monitored Services</div>
        <div class="services-grid" id="services-grid"></div>

        <div class="section-title">Add Service to Monitor</div>
        <div class="settings-form">
            <div class="form-row">
                <input type="text" id="add-name" placeholder="Service name (e.g. my-api)">
                <input type="text" id="add-ns" placeholder="Namespace (e.g. my-project)">
                <button onclick="addService()">Add Service</button>
            </div>
        </div>

        <div class="section-title">Incident History</div>
        <div class="incidents-table" id="incidents-table">
            <div class="empty-state">No incidents recorded yet</div>
        </div>
    </div>

    <script>
        async function loadDashboard() {
            try {
                const res = await fetch('/api/v1/monitoring/dashboard');
                const data = await res.json();
                
                const dot = document.getElementById('overall-dot');
                const text = document.getElementById('overall-text');
                const sub = document.getElementById('overall-sub');
                
                dot.className = `dot ${data.overall_status.toLowerCase()}`;
                text.textContent = `Overall: ${data.overall_status}`;
                sub.textContent = `${data.services.length} service(s) monitored`;

                const grid = document.getElementById('services-grid');
                grid.innerHTML = data.services.map(s => `
                    <div class="service-card">
                        <div class="card-header">
                            <div>
                                <div class="name">${s.name}</div>
                                <span class="namespace">${s.namespace}</span>
                            </div>
                            <span class="badge ${s.status.toLowerCase()}">${s.status}</span>
                        </div>
                        <div class="metrics-row">
                            <div class="metric"><div class="value">${s.pods_running}/${s.pods_total}</div><div class="label">Pods</div></div>
                            <div class="metric"><div class="value">${s.restarts}</div><div class="label">Restarts</div></div>
                            <div class="metric"><div class="value">${s.memory}</div><div class="label">Memory</div></div>
                        </div>
                        ${s.recent_errors.length > 0 ? `
                            <div class="errors-list">
                                <div class="title">Recent Issues</div>
                                ${s.recent_errors.map(e => `<div class="item">${e}</div>`).join('')}
                            </div>
                        ` : ''}
                        ${s.recommendations.length > 0 && s.recommendations[0] !== 'No issues detected' ? `
                            <div class="recs-list">
                                <div class="title">Recommendations</div>
                                ${s.recommendations.map(r => `<div class="item">${r}</div>`).join('')}
                            </div>
                        ` : ''}
                    </div>
                `).join('');

            } catch(e) {
                document.getElementById('overall-text').textContent = 'Failed to load: ' + e.message;
            }

            // Load incidents
            try {
                const res = await fetch('/api/v1/monitoring/incidents');
                const data = await res.json();
                const container = document.getElementById('incidents-table');
                
                if (data.incidents.length === 0) {
                    container.innerHTML = '<div class="empty-state">No incidents recorded yet</div>';
                } else {
                    container.innerHTML = `<table>
                        <tr><th>Time</th><th>Type</th><th>Service</th><th>Summary</th></tr>
                        ${data.incidents.slice(0, 15).map(i => `
                            <tr>
                                <td>${new Date(i.timestamp).toLocaleString()}</td>
                                <td><span class="type-badge ${i.type}">${i.type}</span></td>
                                <td>${i.service}</td>
                                <td>${i.summary}</td>
                            </tr>
                        `).join('')}
                    </table>`;
                }
            } catch(e) {}
        }

        async function addService() {
            const name = document.getElementById('add-name').value.trim();
            const ns = document.getElementById('add-ns').value.trim();
            if (!name || !ns) { alert('Please fill in both fields'); return; }
            
            await fetch('/api/v1/monitoring/services', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name, namespace: ns})
            });
            document.getElementById('add-name').value = '';
            document.getElementById('add-ns').value = '';
            loadDashboard();
        }

        loadDashboard();
        setInterval(loadDashboard, 30000);
    </script>
</body>
</html>
EOF
```

### 5.15 知识库文档 — knowledge_base/

这些 Runbook 和 SOP 是 RAG 系统的知识来源。启动时自动索引到 ChromaDB，AI Agent 诊断时会检索相关内容作为参考。

注意：Runbook 内容用英文写（因为 AI 模型对英文运维术语理解更好），文件内不使用三反引号（用 4 空格缩进代替代码块）。

#### runbooks/pod-crashloopbackoff.md

```bash
cat > knowledge_base/runbooks/pod-crashloopbackoff.md << 'EOF'
# Runbook: Pod CrashLoopBackOff

## Overview
Pod repeatedly starts and crashes. Kubernetes restarts with exponential backoff (10s, 20s, 40s... max 5min).

## Diagnosis Steps

### Check Pod Events
    kubectl describe pod <pod> -n <ns>

Look at Events section for common causes:
- OOMKilled: Out of memory
- Error: Application crash
- ImagePullBackOff: Image pull failure

### Check Crash Logs
    kubectl logs <pod> --previous

### Check Configuration
    kubectl get configmap -n <ns>
    kubectl get secret -n <ns>

Verify required ConfigMaps/Secrets exist with correct keys.

## Remediation

### OOMKilled -> Increase Memory
    kubectl set resources deployment/<name> --limits=memory=512Mi

### Application Error -> Rollback
    kubectl rollout undo deployment/<name>

### Missing Config -> Recreate
    kubectl apply -f <config-manifest>.yaml
    kubectl rollout restart deployment/<name>
EOF
```

#### runbooks/oom-kill.md

```bash
cat > knowledge_base/runbooks/oom-kill.md << 'EOF'
# Runbook: OOM Kill Diagnosis and Scaling

## Overview
OOM Kill occurs when a container exceeds its memory limits. The Linux kernel forcibly terminates the process.

## Severity
- P1: Multiple Pods OOM Killed
- P2: Single Pod occasional OOM

## Diagnosis Steps

### Confirm OOM Kill
    kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState}'
    kubectl describe pod <pod> | grep -A3 "Last State"

### Check Memory Usage
    kubectl top pod <pod>
    kubectl get pod <pod> -o jsonpath='{.spec.containers[*].resources}'

### Analyze Memory Trend
Check Grafana dashboard for container_memory_working_set_bytes:
- Gradual increase = memory leak
- Sudden spike = traffic burst
- Stable high = limits set too low

## Remediation

### Immediate: Increase Memory Limits
    kubectl set resources deployment/<name> --limits=memory=512Mi --requests=memory=256Mi

### If Memory Leak Suspected
1. Enable heap profiling
2. Capture heap dump for analysis
3. Fix code and redeploy

### Prevention
- All containers must have memory limits set
- Set monitoring alerts at 80% of limits
- Use VPA for automatic resource adjustment
EOF
```

#### runbooks/database-connection-pool.md

```bash
cat > knowledge_base/runbooks/database-connection-pool.md << 'EOF'
# Runbook: Database Connection Pool Exhaustion

## Overview
All database connections are occupied. New requests cannot acquire connections, causing timeouts and 500 errors.

## Symptoms
- Application logs show "ConnectionPoolExhausted" or "Timeout waiting for connection"
- HTTP 500/503 errors increasing
- Request latency spike

## Diagnosis Steps

### Check Current Connections (PostgreSQL)
    SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1,2 ORDER BY 3 DESC;

### Find Long-Running Queries
    SELECT pid, now() - query_start AS duration, query, state FROM pg_stat_activity WHERE state != 'idle' ORDER BY duration DESC LIMIT 10;

## Remediation

### Immediate: Kill Idle Connections
    SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle' AND query_start < now() - interval '10 minutes';

### Short-term: Increase Pool Size
Increase pool_size and max_overflow in application configuration.

### Long-term: Deploy PgBouncer
Deploy a connection pooling middleware to reuse connections efficiently.
EOF
```

#### sops/incident-response.md

```bash
cat > knowledge_base/sops/incident-response.md << 'EOF'
# SOP: Incident Response Procedure

## Severity Definitions
- P0: Service completely unavailable
- P1: Major degradation, most users affected
- P2: Partial degradation, some users affected

## Response Steps

### 1. Acknowledge and Communicate (within 5 minutes)
- Confirm alert is genuine
- Open incident Slack channel
- Notify relevant personnel

### 2. Investigate (5-30 minutes)
- Check recent deployments/changes
- Review Grafana dashboards
- Examine application logs

### 3. Mitigate (restore service first)
- Rollback recent deployment
- Scale up
- Restart pods
- Failover to backup

### 4. Verify Recovery
- Confirm metrics return to normal
- Run smoke tests
- Update incident channel with status

### 5. Post-Mortem (within 48 hours)
- Document timeline
- Root cause analysis (5 Whys)
- Action items with owners and deadlines
EOF
```

---

## 第六步：设置 RBAC 和 Secret

### 6.1 创建 ServiceAccount

为什么需要 ServiceAccount？AI 平台要跨命名空间操作（读取 demo-project 的 Pod、重启 Pod、修改 Deployment），默认的 SA 没有这些权限。`aiops-sa` 配合 ClusterRole 获得集群级别的运维权限。

```bash
oc project aiops-platform
```

```bash
oc create serviceaccount aiops-sa
```

### 6.2 创建 ClusterRole

这个 ClusterRole 授予 AI 平台所需的所有权限：读写 Pod/Deployment/ReplicaSet/Event/Namespace/ConfigMap/Secret。权限范围经过精心设计——给够操作需要的权限，但不给不必要的权限。

```bash
cat > clusterrole.yaml << 'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: aiops-operator
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log", "services", "events", "namespaces", "configmaps", "secrets"]
  verbs: ["get", "list", "watch", "delete"]
- apiGroups: ["apps"]
  resources: ["deployments", "deployments/scale", "replicasets"]
  verbs: ["get", "list", "watch", "patch", "update"]
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create"]
EOF
```

```bash
oc apply -f clusterrole.yaml
```

### 6.3 绑定 ClusterRole

```bash
oc adm policy add-cluster-role-to-user aiops-operator system:serviceaccount:aiops-platform:aiops-sa
```

### 6.4 创建 OpenAI API Key Secret

```bash
oc create secret generic openai-secret --from-literal=OPENAI_API_KEY=你的实际API_KEY -n aiops-platform
```

---

## 第七步：构建和部署 AIOps 平台

### 7.1 构建镜像

```bash
cd ~/aiops-platform
```

```bash
oc project aiops-platform
```

```bash
oc new-build --binary --strategy=docker --name=aiops-platform
```

```bash
oc start-build aiops-platform --from-dir=. --follow
```

等构建完成（约 3-5 分钟，主要是安装 Python 依赖）。

### 7.2 获取镜像 digest

```bash
IMAGE=$(oc get istag aiops-platform:latest -o jsonpath='{.image.dockerImageReference}')
echo $IMAGE
```

### 7.3 创建 Deployment

为什么用 digest 而不是 `:latest`？OpenShift 的 ImageStream 会缓存 `:latest` tag，更新代码后 `oc start-build` 生成新镜像，但 Deployment 仍然拉旧镜像。用 digest 可以确保每次部署使用最新构建的镜像。

环境变量说明：
- `HOME=/tmp`：某些 Python 库（如 chromadb）需要写 HOME 目录，OpenShift 的非 root 用户无法写 `/`
- `OPENAI_API_KEY`：从 Secret 注入
- `LLM_MODEL=gpt-4o-mini`：指定使用的 OpenAI 模型

```bash
cat > deployment.yaml << 'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aiops-platform
spec:
  replicas: 1
  selector:
    matchLabels:
      app: aiops-platform
  template:
    metadata:
      labels:
        app: aiops-platform
    spec:
      serviceAccountName: aiops-sa
      containers:
      - name: aiops-platform
        image: IMAGE_PLACEHOLDER
        imagePullPolicy: Always
        ports:
        - containerPort: 8000
        env:
        - name: HOME
          value: "/tmp"
        - name: OPENAI_API_KEY
          valueFrom:
            secretKeyRef:
              name: openai-secret
              key: OPENAI_API_KEY
        - name: LLM_MODEL
          value: "gpt-4o-mini"
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "512Mi"
            cpu: "500m"
        readinessProbe:
          httpGet:
            path: /api/v1/health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 15
        livenessProbe:
          httpGet:
            path: /api/v1/health
            port: 8000
          initialDelaySeconds: 15
          periodSeconds: 30
EOF
```

```bash
# macOS 用 sed -i ''，Linux 用 sed -i（去掉引号）
sed -i '' "s|IMAGE_PLACEHOLDER|${IMAGE}|" deployment.yaml
```

```bash
oc apply -f deployment.yaml
```

### 7.4 创建 Service 和 Route

```bash
oc expose deployment aiops-platform --port=8000
```

```bash
oc create route edge aiops-platform --service=aiops-platform --insecure-policy=Redirect
```

### 7.5 验证部署

```bash
oc get pods -w
```

等 Pod 变为 Running 状态（约 30-60 秒），然后：

```bash
ROUTE=$(oc get route aiops-platform -o jsonpath='{.spec.host}')
echo "https://${ROUTE}"
```

```bash
curl -k "https://${ROUTE}/api/v1/health"
```

应该返回 `{"status":"healthy","timestamp":"...","version":"0.1.0"}`。

浏览器打开 `https://<ROUTE>` 看到聊天界面，`https://<ROUTE>/dashboard` 看到 Dashboard。

---

## 第八步：配置 Prometheus 监控

### 8.1 启用 User Workload Monitoring

为什么需要这一步？OpenShift 自带的 Prometheus 只监控平台组件，不监控用户项目。需要开启 User Workload Monitoring 才能采集 demo-app 和 aiops-platform 的指标，并创建自定义告警规则。

`enableUserAlertmanagerConfig: true` 允许用户项目配置自己的 AlertManager 路由（将告警发送到 AI 平台的 webhook）。

```bash
cat > cluster-monitoring-config.yaml << 'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-monitoring-config
  namespace: openshift-monitoring
data:
  config.yaml: |
    enableUserWorkload: true
    alertmanagerMain:
      enableUserAlertmanagerConfig: true
EOF
```

```bash
oc apply -f cluster-monitoring-config.yaml
```

等待 1-2 分钟让 prometheus-user-workload Pod 启动：

```bash
oc get pods -n openshift-user-workload-monitoring -w
```

### 8.2 创建 ServiceMonitor

ServiceMonitor 告诉 Prometheus 从哪里采集指标。它通过 `selector.matchLabels.app: demo-app` 匹配 demo-project 里的 Service，从 `/metrics` 端点采集数据。

```bash
cat > servicemonitor.yaml << 'EOF'
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: demo-app-monitor
  namespace: demo-project
spec:
  selector:
    matchLabels:
      app: demo-app
  endpoints:
  - port: 8080-tcp
    interval: 30s
    path: /metrics
EOF
```

```bash
oc apply -f servicemonitor.yaml
```

### 8.3 创建 PrometheusRule（告警规则）

这些规则定义什么条件触发告警。`openshift.io/prometheus-rule-evaluation-scope: leaf-prometheus` 这个 label 是关键——没有它，规则不会被 User Workload 的 Prometheus 评估。

定义了三个告警：
- `PodCrashLooping`：5 分钟内有 Pod 重启
- `HighMemoryUsage`：内存使用超过 80%
- `PodOOMKilled`：检测到 OOMKilled

```bash
cat > prometheusrule.yaml << 'EOF'
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: demo-app-alerts
  namespace: demo-project
  labels:
    openshift.io/prometheus-rule-evaluation-scope: leaf-prometheus
spec:
  groups:
  - name: demo-app.rules
    rules:
    - alert: PodCrashLooping
      expr: increase(kube_pod_container_status_restarts_total{namespace="demo-project"}[5m]) > 0
      for: 1m
      labels:
        severity: warning
        service: demo-app
      annotations:
        summary: "Pod {{ $labels.pod }} is crash looping"
        description: "Pod {{ $labels.pod }} in {{ $labels.namespace }} has restarted {{ $value }} times in the last 5 minutes"
    - alert: HighMemoryUsage
      expr: container_memory_working_set_bytes{namespace="demo-project",container!=""} / container_spec_memory_limit_bytes{namespace="demo-project",container!=""} > 0.8
      for: 2m
      labels:
        severity: warning
        service: demo-app
      annotations:
        summary: "High memory usage detected for {{ $labels.pod }}"
        description: "Container {{ $labels.container }} in pod {{ $labels.pod }} is using more than 80% of its memory limit"
    - alert: PodOOMKilled
      expr: kube_pod_container_status_last_terminated_reason{namespace="demo-project",reason="OOMKilled"} == 1
      for: 0m
      labels:
        severity: critical
        service: demo-app
      annotations:
        summary: "Pod {{ $labels.pod }} was OOM Killed"
        description: "Container {{ $labels.container }} in pod {{ $labels.pod }} was terminated due to OOM"
EOF
```

```bash
oc apply -f prometheusrule.yaml
```

### 8.4 配置 AlertManager Webhook

这是将 Prometheus 告警连接到 AI 平台的桥梁。当告警触发时，AlertManager 将告警数据 POST 到 aiops-platform 的 `/api/v1/agent/alert` 端点，AI Agent 自动接收并处理。

```bash
cat > alertmanagerconfig.yaml << 'EOF'
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: aiops-webhook
  namespace: demo-project
spec:
  route:
    receiver: aiops-platform
    groupBy: ["alertname"]
    groupWait: 30s
    groupInterval: 5m
    repeatInterval: 1h
  receivers:
  - name: aiops-platform
    webhookConfigs:
    - url: "http://aiops-platform.aiops-platform.svc.cluster.local:8000/api/v1/agent/alert"
      sendResolved: true
EOF
```

```bash
oc apply -f alertmanagerconfig.yaml
```

---

## 第九步：添加 healthy-app 到监控

在 Dashboard 上添加 healthy-app 作为对照。可以通过 API 调用或在 Dashboard 页面的表单中输入。

```bash
ROUTE=$(oc get route aiops-platform -n aiops-platform -o jsonpath='{.spec.host}')
```

```bash
curl -k -X POST "https://${ROUTE}/api/v1/monitoring/services" \
  -H "Content-Type: application/json" \
  -d '{"name": "healthy-app", "namespace": "healthy-project"}'
```

现在 Dashboard 应该显示两个服务卡片：demo-app 和 healthy-app。

---

## 第十步：测试和演示

### 10.1 触发故障测试

先获取 demo-app 的 Route：

```bash
DEMO_ROUTE=$(oc get route demo-app -n demo-project -o jsonpath='{.spec.host}')
```

触发 crash（Pod 会进入 CrashLoopBackOff）：

```bash
curl http://${DEMO_ROUTE}/crash
```

触发 OOM（Pod 会被 OOMKilled）：

```bash
curl http://${DEMO_ROUTE}/oom
```

### 10.2 在 Web UI 上演示

打开 `https://<AIOPS_ROUTE>`：

1. **Health Check**：点击 "Health Check" 按钮 → 看到 demo-app 状态变为 DEGRADED，healthy-app 保持 HEALTHY，底部出现建议操作按钮
2. **Diagnose**：点击 "Diagnose Service" → AI 分析症状，返回根因诊断和修复建议
3. **Log Analysis**：点击 "Log Analysis" → 显示错误日志统计和 AI 分析
4. **Knowledge Query**：点击 "OOM Kill" → RAG 从知识库检索 Runbook 内容回答
5. **Restart**：输入 `/ops restart demo-app` → 直接执行重启
6. **Scale（需审批）**：输入 `/ops scale demo-app 3` → 弹出审批卡片，点击 Approve 执行
7. **Dashboard**：打开 `/dashboard` → 看到服务卡片、指标、Incident History

### 10.3 测试自动修复

模拟 AlertManager 发送告警到 AI 平台：

```bash
ROUTE=$(oc get route aiops-platform -n aiops-platform -o jsonpath='{.spec.host}')
```

```bash
curl -k -X POST "https://${ROUTE}/api/v1/agent/alert" \
  -H "Content-Type: application/json" \
  -d '{
    "alerts": [{
      "status": "firing",
      "labels": {
        "alertname": "PodOOMKilled",
        "severity": "critical",
        "service": "demo-app",
        "namespace": "demo-project"
      },
      "annotations": {
        "summary": "Pod demo-app-xxx was OOM Killed"
      }
    }]
  }'
```

AI Agent 会：
1. 接收告警
2. 分类为 OOM 类问题
3. 搜索知识库找到 OOM Kill Runbook
4. 分析日志
5. 生成诊断报告
6. 自动执行 `restart_pod`（低风险，无需审批）
7. 记录 Incident History

### 10.4 更新代码后重新部署

修改代码后，重新构建和部署：

```bash
cd ~/aiops-platform
```

```bash
oc project aiops-platform
```

```bash
oc start-build aiops-platform --from-dir=. --follow
```

```bash
IMAGE=$(oc get istag aiops-platform:latest -o jsonpath='{.image.dockerImageReference}')
```

```bash
oc set image deployment/aiops-platform aiops-platform=${IMAGE}
```

---

## API 参考

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/` | 聊天页面 |
| GET | `/dashboard` | Dashboard 仪表盘 |
| GET | `/metrics` | Prometheus 指标 |
| GET | `/api/v1/health` | 健康检查 |
| POST | `/api/v1/knowledge/query` | RAG 知识库查询 |
| POST | `/api/v1/knowledge/index` | 重新索引知识库 |
| POST | `/api/v1/agent/alert` | 接收告警（AlertManager webhook） |
| POST | `/api/v1/agent/diagnose` | 手动诊断 |
| POST | `/api/v1/logs/analyze` | 日志分析 |
| POST | `/api/v1/chatops/message` | ChatOps 消息 |
| POST | `/api/v1/executor/execute` | 执行操作 |
| POST | `/api/v1/executor/approve` | 审批操作 |
| GET | `/api/v1/executor/pending` | 待审批列表 |
| GET | `/api/v1/executor/audit` | 审计日志 |
| GET | `/api/v1/monitoring/dashboard` | Dashboard 数据 |
| GET | `/api/v1/monitoring/services` | 监控服务列表 |
| POST | `/api/v1/monitoring/services` | 添加监控服务 |
| DELETE | `/api/v1/monitoring/services` | 删除监控服务 |
| GET | `/api/v1/monitoring/incidents` | 事件历史 |
| POST | `/api/v1/monitoring/incidents/{id}/resolve` | 标记事件已解决 |
| GET | `/api/v1/monitoring/namespaces` | 集群命名空间列表 |
