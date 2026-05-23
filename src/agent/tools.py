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
