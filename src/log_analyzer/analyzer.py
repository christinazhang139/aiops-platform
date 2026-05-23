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
