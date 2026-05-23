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
