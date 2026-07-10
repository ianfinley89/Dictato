"""User-facing issue reports. The frontend auto-attaches context (last capture,
pane, user agent) so a one-line complaint still carries enough to debug. Each
report is auto-triaged (app/services/triage.py) into a pipeline: infra bugs,
model-quality reports (dataset candidates), or capture/device failures."""
import json

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user_id
from app.database import get_conn
from app.services import triage

router = APIRouter(prefix="/api/issues", tags=["issues"])

_DAILY_REPORT_CAP = 20   # bounds abuse and the (tiny) triage-call cost


class IssueCreate(BaseModel):
    message: str
    context: dict | None = None
    capture_id: int | None = None   # the capture the report is about, when known


@router.post("/")
async def create_issue(request: Request, body: IssueCreate):
    uid = get_current_user_id(request)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Describe the issue first.")
    if len(message) > 2000:
        raise HTTPException(413, "Keep it under 2000 characters.")
    context = json.dumps(body.context or {}, default=str)[:8000]

    capture_id = body.capture_id
    with get_conn() as conn:
        today = conn.execute(
            "SELECT COUNT(*) AS n FROM issue_reports WHERE user_id=? AND created_at >= date('now')",
            (uid,),
        ).fetchone()["n"]
        if today >= _DAILY_REPORT_CAP:
            raise HTTPException(429, "That's plenty of reports for one day — thank you! Try again tomorrow.")
        # A report may only link to the reporter's own capture.
        if capture_id is not None:
            row = conn.execute("SELECT user_id FROM capture_log WHERE id=?", (capture_id,)).fetchone()
            if not row or row["user_id"] != uid:
                capture_id = None

    # Triage outside the write lock; None just means "admin labels it later".
    category = await triage.classify_issue(message, body.context, user_id=uid)

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO issue_reports (user_id, message, context_json, category, capture_id)
               VALUES (?,?,?,?,?)""",
            (uid, message, context, category, capture_id),
        )
    return {"ok": True, "category": category}
