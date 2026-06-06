"""Web Push delivery via VAPID. pywebpush is synchronous (uses requests), so
callers should invoke `send_push` through asyncio.to_thread to avoid blocking."""
import json
from pywebpush import webpush, WebPushException
from app.config import VAPID_PRIVATE_KEY, VAPID_SUBJECT
from app.database import get_conn


def send_push(subscription_info: dict, payload: dict) -> bool:
    """Send one push. Returns True on success. On 404/410 the subscription is
    dead and is removed from the DB."""
    if not VAPID_PRIVATE_KEY:
        return False
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
        )
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            endpoint = subscription_info.get("endpoint")
            with get_conn() as conn:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        return False


def send_to_user(user_id: int, payload: dict) -> int:
    """Send a payload to every subscription a user has. Returns count sent."""
    with get_conn() as conn:
        subs = conn.execute(
            "SELECT endpoint, keys_json FROM push_subscriptions WHERE user_id=?", (user_id,)
        ).fetchall()
    sent = 0
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": json.loads(s["keys_json"])}
        if send_push(info, payload):
            sent += 1
    return sent
