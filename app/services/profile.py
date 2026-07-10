"""The durable user profile: facts accumulated from anything the user says —
coach conversations AND passing mentions while logging food ("after I rode my
bike to school"). Both the coach and the logging agent write here."""
import json
from datetime import datetime, timezone

from app.database import get_conn


def merge_facts(old: dict, new: dict) -> dict:
    """Lists append (weigh-ins accumulate), dicts merge, scalars overwrite."""
    out = dict(old)
    for k, v in (new or {}).items():
        if isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def get_profile(uid: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT facts_json FROM user_profile WHERE user_id=?", (uid,)).fetchone()
    return json.loads(row["facts_json"]) if row else {}


def apply_profile_update(uid: int, facts: dict) -> None:
    if not facts:
        return
    merged = merge_facts(get_profile(uid), facts)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO user_profile (user_id, facts_json, updated_at) VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET facts_json=excluded.facts_json, updated_at=excluded.updated_at""",
            (uid, json.dumps(merged), now),
        )
