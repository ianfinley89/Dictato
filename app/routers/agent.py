"""One endpoint for voice/photo/text logging: transcribe if needed, try the
zero-cost fast path, otherwise run the agent tool loop. Entries are saved
server-side; the frontend shows the result with per-entry Undo/Adjust."""
import json
import os
import uuid
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form

from app.auth import get_current_user_id
from app.services import agent, stt
from app.services.ai_usage import check_and_increment, get_today_usage
from app.services.logging import current_entries
from app.database import get_conn
from app.config import ANTHROPIC_API_KEY, AI_DAILY_LIMIT

router = APIRouter(prefix="/api/agent", tags=["agent"])

_IMG_ALLOWED = {"image/jpeg", "image/png", "image/webp"}
_IMG_MAX = 5 * 1024 * 1024    # client compresses to ~1024px first
_AUDIO_MAX = 15 * 1024 * 1024  # a couple minutes of webm/opus or AAC

# Keep only the analysis-relevant fields of each logged entry in the capture log.
# (`id` = log_entries id, so later analysis can tell which entries were undone.)
_CAPTURE_ENTRY_KEYS = (
    "id", "food_name", "food_brand", "quantity_g", "food_source",
    "calories", "protein_g", "carbs_g", "fat_g", "fiber_g",
)


def _record_capture(uid, input_type, transcript, summary, entries, fast_path,
                    annotation=None, photo_path=None, audio_path=None,
                    parent_capture_id=None):
    """Persist every capture verbatim for later analysis / coaching / datasets.
    Best-effort: a logging failure must never break the actual food logging."""
    try:
        a = annotation or {}
        slim = [{k: e.get(k) for k in _CAPTURE_ENTRY_KEYS if k in e} for e in (entries or [])]
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO capture_log
                   (user_id, input_type, transcript, summary, entries_json, fast_path,
                    meal, meal_label, tags_json, specificity, photo_path, audio_path,
                    parent_capture_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, input_type, transcript, summary, json.dumps(slim), 1 if fast_path else 0,
                 a.get("meal"), a.get("meal_label"), json.dumps(a.get("tags") or []),
                 a.get("specificity"), photo_path, audio_path, parent_capture_id),
            )
            return cur.lastrowid
    except Exception:
        return None


def _save_capture_photo(image_bytes: bytes, image_type: str) -> str | None:
    """Keep the compressed capture photo on disk — it's the image half of the
    photo→label training pairs. Lives outside the web root; deleted with the
    account. Best-effort."""
    try:
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(image_type, "jpg")
        d = os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "captures")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{uuid.uuid4().hex}.{ext}")
        with open(path, "wb") as f:
            f.write(image_bytes)
        return path
    except Exception:
        return None


_AUDIO_EXT = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/mpeg": "mp3",
              "audio/ogg": "ogg", "audio/wav": "wav"}


def _save_capture_audio(audio_bytes: bytes, content_type: str | None) -> str | None:
    """Keep the raw voice note on disk — the audio half of the voice→label
    training pairs, and the only way to replay an STT failure (e.g. a phone
    mic recording near-silence). Same lifecycle as photos: outside the web
    root, deleted with the account. Best-effort."""
    try:
        base = (content_type or "").split(";")[0].strip().lower()
        ext = _AUDIO_EXT.get(base, "webm")
        d = os.path.join(os.getenv("UPLOAD_DIR", "uploads"), "captures", "audio")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{uuid.uuid4().hex}.{ext}")
        with open(path, "wb") as f:
            f.write(audio_bytes)
        return path
    except Exception:
        return None


@router.post("/log")
async def agent_log(
    request: Request,
    text: str | None = Form(None),
    tz_offset: int = Form(0),
    revise_capture_id: int | None = Form(None),
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

    audio_path = None
    if audio is not None:
        blob = await audio.read()
        if not blob:
            raise HTTPException(400, "Empty audio upload")
        if len(blob) > _AUDIO_MAX:
            raise HTTPException(413, "Recording too long.")
        audio_path = _save_capture_audio(blob, audio.content_type)
        # STT failures still get a capture row (with the audio attached): a mic
        # that records near-silence is exactly the failure we want to replay
        # later, not one that vanishes into a 422.
        try:
            transcript = await stt.transcribe(blob)
        except Exception:
            _record_capture(uid, "voice", None, "(couldn't decode the audio)", [],
                            fast_path=False, audio_path=audio_path)
            raise HTTPException(422, "Couldn't decode the audio recording. Try again.")
        if not transcript:
            _record_capture(uid, "voice", None, "(no speech detected)", [],
                            fast_path=False, audio_path=audio_path)
            raise HTTPException(422, "Didn't catch any speech — try again.")
        input_type = "voice"

    if not transcript and not image_bytes:
        raise HTTPException(400, "Provide text, audio, or an image.")

    # Follow-up refinement ("say more" / "add photo" after a log): load the
    # original capture and let the agent reconcile it with the new context.
    revision = None
    if revise_capture_id is not None:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT user_id, transcript, entries_json FROM capture_log WHERE id=?",
                (revise_capture_id,),
            ).fetchone()
        if not row or row["user_id"] != uid:
            raise HTTPException(404, "Capture not found")
        try:
            original_ids = [e["id"] for e in json.loads(row["entries_json"] or "[]") if e.get("id")]
        except json.JSONDecodeError:
            original_ids = []
        revision = {"transcript": row["transcript"],
                    "entries": current_entries(uid, original_ids),
                    "original_ids": original_ids}

    # Zero-cost fast path: every item matches a food this user already knows.
    # (Revisions always need the model — they reconcile, not just match.)
    if transcript and not image_bytes and revision is None:
        entries = agent.fast_path_log(uid, transcript, method)
        if entries:
            names = ", ".join(e["food_name"] for e in entries)
            summary = f"Logged {names}."
            annotation = agent.fast_path_annotation(transcript, entries, tz_offset)
            capture_id = _record_capture(uid, input_type, transcript, summary, entries,
                                         fast_path=True, annotation=annotation,
                                         audio_path=audio_path)
            return {"capture_id": capture_id, "transcript": transcript, "summary": summary,
                    "entries": entries, "annotation": annotation, "fast_path": True}

    if not ANTHROPIC_API_KEY:
        raise HTTPException(503, "AI logging is not configured.")
    if not check_and_increment(uid):
        raise HTTPException(429, f"Daily AI limit ({AI_DAILY_LIMIT} calls) reached. Try again tomorrow.")

    result = await agent.run_agent(
        uid, text=transcript, image=image_bytes,
        image_media_type=image_type, method=method, tz_offset=tz_offset,
        revision=revision,
    )

    photo_path = _save_capture_photo(image_bytes, image_type) if image_bytes else None

    if revision is not None:
        # Final state = the surviving originals plus anything newly logged.
        all_ids = revision["original_ids"] + [e["id"] for e in result["entries"]]
        entries = current_entries(uid, all_ids)
        capture_id = _record_capture(uid, input_type, transcript, result["summary"], entries,
                                     fast_path=False, annotation=result.get("annotation"),
                                     photo_path=photo_path, audio_path=audio_path,
                                     parent_capture_id=revise_capture_id)
        # A photo follow-up has no words of its own — keep showing the original
        # transcript so the context reads as appended, not replaced.
        return {"capture_id": capture_id,
                "transcript": transcript or revision.get("transcript"),
                "summary": result["summary"],
                "entries": entries, "annotation": result.get("annotation") or {},
                "fast_path": False, "revised": True}

    capture_id = _record_capture(uid, input_type, transcript, result["summary"], result["entries"],
                                 fast_path=False, annotation=result.get("annotation"),
                                 photo_path=photo_path, audio_path=audio_path)
    return {"capture_id": capture_id, "transcript": transcript, "summary": result["summary"],
            "entries": result["entries"], "annotation": result.get("annotation") or {},
            "fast_path": False}


@router.get("/usage")
async def usage(request: Request):
    uid = get_current_user_id(request)
    return {**get_today_usage(uid), "daily_limit": AI_DAILY_LIMIT}
