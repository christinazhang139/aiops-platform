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
