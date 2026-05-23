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
