"""User-facing issue reports. The frontend auto-attaches context (last capture,
pane, user agent) so a one-line complaint still carries enough to debug."""
import json

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from app.auth import get_current_user_id
from app.database import get_conn

router = APIRouter(prefix="/api/issues", tags=["issues"])


class IssueCreate(BaseModel):
    message: str
    context: dict | None = None


@router.post("/")
async def create_issue(request: Request, body: IssueCreate):
    uid = get_current_user_id(request)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Describe the issue first.")
    if len(message) > 2000:
        raise HTTPException(413, "Keep it under 2000 characters.")
    context = json.dumps(body.context or {}, default=str)[:8000]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO issue_reports (user_id, message, context_json) VALUES (?,?,?)",
            (uid, message, context),
        )
    return {"ok": True}
