from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from app.auth import get_current_user_id
from app.services import ai
from app.services.ai_usage import check_and_increment
from app.config import ANTHROPIC_API_KEY, AI_DAILY_LIMIT

router = APIRouter(prefix="/api/photo", tags=["photo"])

_ALLOWED = {"image/jpeg", "image/png", "image/webp"}
_MAX_BYTES = 5 * 1024 * 1024  # client compresses to ~1024px; 5MB is a generous cap


@router.post("/parse")
async def parse_photo(request: Request, image: UploadFile = File(...)):
    uid = get_current_user_id(request)

    if image.content_type not in _ALLOWED:
        raise HTTPException(415, "Unsupported image type. Use JPEG, PNG, or WebP.")
    data = await image.read()
    if not data:
        raise HTTPException(400, "Empty upload")
    if len(data) > _MAX_BYTES:
        raise HTTPException(413, "Image too large (max 5MB). Compress before upload.")

    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "Photo recognition is not configured.")

    if not check_and_increment(uid):
        raise HTTPException(429, f"Daily AI limit ({AI_DAILY_LIMIT} calls) reached. Try again tomorrow.")

    try:
        result = await ai.parse_image(data, image.content_type, uid)
    except Exception:
        raise HTTPException(502, "Could not analyze the photo. Try again.")

    if not result["items"]:
        raise HTTPException(422, "No food recognized in the photo.")
    return {"items": result["items"], "source": "haiku", "summary": result["summary"]}
