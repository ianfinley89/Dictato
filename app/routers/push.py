import json
import asyncio
from fastapi import APIRouter, Request, HTTPException
from app.auth import get_current_user_id
from app.models import PushSubscription, Unsubscribe
from app.database import get_conn
from app.config import VAPID_PUBLIC_KEY
from app.services.push import send_to_user

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid-key")
async def vapid_key():
    return {"public_key": VAPID_PUBLIC_KEY}


@router.post("/subscribe")
async def subscribe(req: PushSubscription, request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        # One row per endpoint; re-subscribing updates the keys/owner.
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (req.endpoint,))
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, keys_json) VALUES (?,?,?)",
            (uid, req.endpoint, json.dumps(req.keys)),
        )
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(req: Unsubscribe, request: Request):
    uid = get_current_user_id(request)
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?", (req.endpoint, uid)
        )
    return {"ok": True}


@router.post("/test")
async def test_push(request: Request):
    uid = get_current_user_id(request)
    payload = {"title": "Dictato", "body": "🔔 Push notifications are working!", "url": "/"}
    sent = await asyncio.to_thread(send_to_user, uid, payload)
    if not sent:
        raise HTTPException(400, "No active subscriptions (enable notifications first).")
    return {"sent": sent}
