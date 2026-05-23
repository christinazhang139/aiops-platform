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
