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
