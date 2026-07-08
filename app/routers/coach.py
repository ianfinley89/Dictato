"""On-demand coaching chat. Reads the user's logs, notes, goals, and profile
and replies with grounded suggestions; remembers durable facts as it goes.
Rate-limited against the shared daily AI cap (this is a paid model call)."""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user_id
from app.services import coach
from app.services.ai_usage import check_and_increment
from app.config import ANTHROPIC_API_KEY, AI_DAILY_LIMIT

router = APIRouter(prefix="/api/coach", tags=["coach"])


class CoachMessage(BaseModel):
    message: str


@router.get("/history")
async def history(request: Request):
    uid = get_current_user_id(request)
    return {"messages": coach.get_history(uid), "profile": coach.get_profile(uid)}


@router.post("/chat")
async def chat(request: Request, body: CoachMessage):
    uid = get_current_user_id(request)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Empty message")
    if len(message) > 2000:
        raise HTTPException(413, "Message too long.")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "The coach is not configured.")
    if not check_and_increment(uid):
        raise HTTPException(429, f"Daily AI limit ({AI_DAILY_LIMIT} calls) reached. Try again tomorrow.")

    try:
        return await coach.chat(uid, message)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "The coach couldn't respond just now. Try again.")
