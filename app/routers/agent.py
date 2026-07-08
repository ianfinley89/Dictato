"""One endpoint for voice/photo/text logging: transcribe if needed, try the
zero-cost fast path, otherwise run the agent tool loop. Entries are saved
server-side; the frontend shows the result with per-entry Undo/Adjust."""
import json
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form

from app.auth import get_current_user_id
from app.services import agent, stt
from app.services.ai_usage import check_and_increment, get_today_usage
from app.database import get_conn
from app.config import ANTHROPIC_API_KEY, AI_DAILY_LIMIT

router = APIRouter(prefix="/api/agent", tags=["agent"])

_IMG_ALLOWED = {"image/jpeg", "image/png", "image/webp"}
_IMG_MAX = 5 * 1024 * 1024    # client compresses to ~1024px first
_AUDIO_MAX = 15 * 1024 * 1024  # a couple minutes of webm/opus or AAC

# Keep only the analysis-relevant fields of each logged entry in the capture log.
_CAPTURE_ENTRY_KEYS = (
    "food_name", "food_brand", "quantity_g", "food_source",
    "calories", "protein_g", "carbs_g", "fat_g", "fiber_g",
)


def _record_capture(uid, input_type, transcript, summary, entries, fast_path):
    """Persist every capture verbatim for later analysis / coaching. Best-effort:
    a logging failure must never break the actual food logging."""
    try:
        slim = [{k: e.get(k) for k in _CAPTURE_ENTRY_KEYS if k in e} for e in (entries or [])]
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO capture_log
                   (user_id, input_type, transcript, summary, entries_json, fast_path)
                   VALUES (?,?,?,?,?,?)""",
                (uid, input_type, transcript, summary, json.dumps(slim), 1 if fast_path else 0),
            )
    except Exception:
        pass


@router.post("/log")
async def agent_log(
    request: Request,
    text: str | None = Form(None),
    audio: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
):
    uid = get_current_user_id(request)

    transcript = (text or "").strip() or None
    image_bytes = None
    image_type = "image/jpeg"
    method = "voice"
    input_type = "text"   # capture-log classification; refined below

    if image is not None:
        if image.content_type not in _IMG_ALLOWED:
            raise HTTPException(415, "Unsupported image type. Use JPEG, PNG, or WebP.")
        image_bytes = await image.read()
        if not image_bytes:
            raise HTTPException(400, "Empty image upload")
        if len(image_bytes) > _IMG_MAX:
            raise HTTPException(413, "Image too large (max 5MB). Compress before upload.")
        image_type = image.content_type
        method = "photo"
        input_type = "photo"

    if audio is not None:
        blob = await audio.read()
        if not blob:
            raise HTTPException(400, "Empty audio upload")
        if len(blob) > _AUDIO_MAX:
            raise HTTPException(413, "Recording too long.")
        try:
            transcript = await stt.transcribe(blob)
        except Exception:
            raise HTTPException(422, "Couldn't decode the audio recording. Try again.")
        if not transcript:
            raise HTTPException(422, "Didn't catch any speech — try again.")
        input_type = "voice"

    if not transcript and not image_bytes:
        raise HTTPException(400, "Provide text, audio, or an image.")

    # Zero-cost fast path: every item matches a food this user already knows.
    if transcript and not image_bytes:
        entries = agent.fast_path_log(uid, transcript, method)
        if entries:
            names = ", ".join(e["food_name"] for e in entries)
            summary = f"Logged {names}."
            _record_capture(uid, input_type, transcript, summary, entries, fast_path=True)
            return {"transcript": transcript, "summary": summary,
                    "entries": entries, "fast_path": True}

    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "AI logging is not configured.")
    if not check_and_increment(uid):
        raise HTTPException(429, f"Daily AI limit ({AI_DAILY_LIMIT} calls) reached. Try again tomorrow.")

    result = await agent.run_agent(
        uid, text=transcript, image=image_bytes,
        image_media_type=image_type, method=method,
    )
    _record_capture(uid, input_type, transcript, result["summary"], result["entries"], fast_path=False)
    return {"transcript": transcript, "summary": result["summary"],
            "entries": result["entries"], "fast_path": False}


@router.get("/usage")
async def usage(request: Request):
    uid = get_current_user_id(request)
    return {**get_today_usage(uid), "daily_limit": AI_DAILY_LIMIT}
