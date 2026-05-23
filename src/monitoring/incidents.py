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
