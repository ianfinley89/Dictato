from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.auth import get_current_user_id
from app.services.voice_parse import parse_local
from app.services import ai
from app.services.ai_usage import check_and_increment, get_today_usage
from app.config import ANTHROPIC_API_KEY, AI_DAILY_LIMIT

router = APIRouter(prefix="/api/voice", tags=["voice"])

_MIN_CONFIDENCE = 0.5  # below this on any item triggers Haiku fallback


class VoiceRequest(BaseModel):
    transcript: str


@router.post("/parse")
async def parse_voice(request: Request, body: VoiceRequest):
    uid = get_current_user_id(request)
    transcript = body.transcript.strip()
    if not transcript:
        raise HTTPException(400, "Empty transcript")

    items = parse_local(transcript)
    needs_ai = not items or any(i["confidence"] < _MIN_CONFIDENCE for i in items)

    if not needs_ai or not ANTHROPIC_API_KEY:
        return {"items": items or _fallback(transcript), "source": "local", "summary": ""}

    if not check_and_increment(uid):
        raise HTTPException(429, f"Daily AI limit ({AI_DAILY_LIMIT} calls) reached. Try again tomorrow.")

    try:
        result = await ai.parse_text(transcript, uid)
        ai_items = result["items"]
        return {
            "items": ai_items or items or _fallback(transcript),
            "source": "haiku",
            "summary": result["summary"] if ai_items else "",
        }
    except Exception:
        return {"items": items or _fallback(transcript), "source": "local", "summary": ""}


@router.get("/usage")
async def usage(request: Request):
    uid = get_current_user_id(request)
    return {**get_today_usage(uid), "daily_limit": AI_DAILY_LIMIT}


def _fallback(transcript: str) -> list[dict]:
    return [{"name": transcript, "brand": None, "est_quantity_g": 100.0, "est_servings": 1,
             "unit": "assumed_100g", "confidence": 0.3}]
