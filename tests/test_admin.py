"""Admin usage dashboard: access control and aggregate math."""
import json

import pytest

from app.database import get_conn
# Bound at import time, before the autouse no_live_triage fixture patches the
# module attribute — this is the REAL function, for unit-testing triage itself.
from app.services.triage import classify_issue as _real_classify_issue

ADMIN = {"email": "boss@example.com", "password": "password123", "display_name": "Boss"}
FRIEND = {"email": "friend@example.com", "password": "password123", "display_name": "Friend"}


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    monkeypatch.setattr("app.routers.admin.ADMIN_EMAILS", {"boss@example.com"})
    monkeypatch.setattr("app.routers.auth.ADMIN_EMAILS", {"boss@example.com"})


def _seed_capture(uid, input_type="voice", transcript="I had a rice cake",
                  entries='[{"food_name": "rice cake"}]', fast_path=0):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO capture_log (user_id, input_type, transcript, summary, entries_json, fast_path)
               VALUES (?,?,?,?,?,?)""",
            (uid, input_type, transcript, "s", entries, fast_path),
        )


def _uid(email):
    with get_conn() as conn:
        return conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def test_admin_stats_requires_admin(client):
    client.post("/api/auth/register", json=FRIEND)
    assert client.get("/api/admin/stats").status_code == 403
    assert client.get("/api/admin/failures").status_code == 403


def test_me_reports_admin_flag(client):
    client.post("/api/auth/register", json=ADMIN)
    assert client.get("/api/auth/me").json()["is_admin"] is True
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=FRIEND)
    assert client.get("/api/auth/me").json()["is_admin"] is False


def test_admin_stats_aggregates(client):
    client.post("/api/auth/register", json=FRIEND)
    fid = _uid(FRIEND["email"])
    _seed_capture(fid, "voice", fast_path=1)
    _seed_capture(fid, "photo", entries="[]")          # a failure
    _seed_capture(fid, "text")

    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=ADMIN)
    s = client.get("/api/admin/stats?days=14").json()

    t = s["totals"]
    assert t["captures"] == 3
    assert t["active_users"] == 1
    assert t["fast_path_pct"] == pytest.approx(33.3, abs=0.1)
    assert t["zero_entry_pct"] == pytest.approx(33.3, abs=0.1)
    assert s["daily"][0]["voice"] == 1
    assert s["daily"][0]["photo"] == 1
    assert s["daily"][0]["text"] == 1
    # per-user table includes both users, friend has the captures
    friend_row = next(u for u in s["per_user"] if u["display_name"] == "Friend")
    assert friend_row["captures"] == 3


def test_admin_failures_feed(client):
    client.post("/api/auth/register", json=FRIEND)
    fid = _uid(FRIEND["email"])
    _seed_capture(fid, "voice", transcript="log my bench press pr", entries="[]")
    _seed_capture(fid, "voice", transcript="I had toast")           # success, excluded

    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=ADMIN)
    f = client.get("/api/admin/failures").json()["failures"]
    assert len(f) == 1
    assert f[0]["transcript"] == "log my bench press pr"
    assert f[0]["display_name"] == "Friend"


# ── Issue reports ─────────────────────────────────────────────────────────────

def test_issue_report_roundtrip(client):
    client.post("/api/auth/register", json=FRIEND)
    r = client.post("/api/issues/", json={"message": "It logged tacos as taco shells",
                                          "context": {"transcript": "3 tacos"}})
    assert r.status_code == 200

    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=ADMIN)
    issues = client.get("/api/admin/issues").json()["issues"]
    assert len(issues) == 1
    assert issues[0]["message"] == "It logged tacos as taco shells"
    assert issues[0]["display_name"] == "Friend"
    assert "3 tacos" in issues[0]["context_json"]


def test_issue_report_rejects_empty(client):
    client.post("/api/auth/register", json=FRIEND)
    assert client.post("/api/issues/", json={"message": "   "}).status_code == 400


def test_issue_report_requires_auth(client):
    assert client.post("/api/issues/", json={"message": "hi"}).status_code == 401


# ── Issue triage pipeline (auto-categorize → route to the right queue) ────────

def test_issue_autotriage_stores_category(client, monkeypatch):
    async def fake(message, context=None, user_id=None):
        return "infra"
    monkeypatch.setattr("app.services.triage.classify_issue", fake)
    client.post("/api/auth/register", json=FRIEND)
    r = client.post("/api/issues/", json={"message": "the goals setting won't save"})
    assert r.status_code == 200
    assert r.json()["category"] == "infra"

    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=ADMIN)
    issues = client.get("/api/admin/issues").json()["issues"]
    assert issues[0]["category"] == "infra"


def test_issue_links_own_capture_but_drops_foreign(client, monkeypatch):
    client.post("/api/auth/register", json=ADMIN)      # someone else's capture
    _seed_capture(_uid(ADMIN["email"]))
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=FRIEND)
    _seed_capture(_uid(FRIEND["email"]))
    with get_conn() as conn:
        admin_cap, friend_cap = [r["id"] for r in
                                 conn.execute("SELECT id FROM capture_log ORDER BY id")]

    client.post("/api/issues/", json={"message": "mine", "capture_id": friend_cap})
    client.post("/api/issues/", json={"message": "not mine", "capture_id": admin_cap})
    with get_conn() as conn:
        rows = conn.execute("SELECT message, capture_id FROM issue_reports ORDER BY id").fetchall()
    assert rows[0]["capture_id"] == friend_cap
    assert rows[1]["capture_id"] is None               # foreign link silently dropped


def test_issue_daily_cap(client):
    client.post("/api/auth/register", json=FRIEND)
    uid = _uid(FRIEND["email"])
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO issue_reports (user_id, message) VALUES (?,?)",
            [(uid, f"report {i}") for i in range(20)],
        )
    assert client.post("/api/issues/", json={"message": "one more"}).status_code == 429


def test_admin_relabels_issue_category(client):
    client.post("/api/auth/register", json=FRIEND)
    client.post("/api/issues/", json={"message": "hm"})   # triage stubbed → NULL
    client.post("/api/auth/logout", json={})
    client.post("/api/auth/register", json=ADMIN)

    issue_id = client.get("/api/admin/issues").json()["issues"][0]["id"]
    r = client.post(f"/api/admin/issues/{issue_id}/category", json={"category": "model"})
    assert r.status_code == 200
    assert client.get("/api/admin/issues").json()["issues"][0]["category"] == "model"

    assert client.post(f"/api/admin/issues/{issue_id}/category",
                       json={"category": "bogus"}).status_code == 400
    assert client.post("/api/admin/issues/99999/category",
                       json={"category": "infra"}).status_code == 404


def test_relabel_requires_admin(client):
    client.post("/api/auth/register", json=FRIEND)
    r = client.post("/api/admin/issues/1/category", json={"category": "infra"})
    assert r.status_code == 403


def test_classify_issue_parses_model_reply(monkeypatch):
    """The real triage function (unit level): category word extracted from the
    reply; junk replies → None; context lines make it into the prompt."""
    import asyncio

    sent = {}

    def fake_chat_returning(text):
        async def fake_chat(**kw):
            sent.update(kw)
            from app.services.llm import LLMResponse
            return LLMResponse(text=text, tool_calls=[], stop_reason="end",
                               input_tokens=1, output_tokens=1)
        return fake_chat

    monkeypatch.setattr("app.services.llm.chat", fake_chat_returning("capture"))
    got = asyncio.run(_real_classify_issue("it only heard 'thank you'",
                                           {"transcript": "thank you"}, user_id=1))
    assert got == "capture"
    assert "thank you" in sent["messages"][0]["text"]
    assert sent["feature"] == "issue_triage"

    monkeypatch.setattr("app.services.llm.chat", fake_chat_returning("Category: model."))
    assert asyncio.run(_real_classify_issue("wrong wings", None)) == "model"

    monkeypatch.setattr("app.services.llm.chat", fake_chat_returning("no idea, sorry"))
    assert asyncio.run(_real_classify_issue("???", None)) is None


# ── Model traces ──────────────────────────────────────────────────────────────

def test_llm_chat_records_trace(client, monkeypatch):
    """The real chat() wrapper records a trace row around the provider call."""
    import asyncio
    from app.services import llm

    async def fake_provider(feature, system, messages, tools, max_tokens):
        return llm.LLMResponse(text="hello!", tool_calls=[], stop_reason="end",
                               input_tokens=11, output_tokens=7, raw=None)

    monkeypatch.setattr(llm, "_chat_anthropic", fake_provider)
    client.post("/api/auth/register", json=ADMIN)
    uid = _uid(ADMIN["email"])

    asyncio.run(llm.chat(feature="coach", system="sys", user_id=uid,
                         messages=[{"role": "user", "text": "hi"}]))

    traces = client.get("/api/admin/traces").json()["traces"]
    assert len(traces) == 1
    t = traces[0]
    assert (t["feature"], t["provider"]) == ("coach", "anthropic")
    assert t["input_tokens"] == 11 and t["output_tokens"] == 7
    assert t["error"] is None
    assert "hello!" in t["response_json"]
    assert t["latency_ms"] is not None


def test_llm_chat_records_trace_on_failure(client, monkeypatch):
    import asyncio
    import pytest as _pytest
    from app.services import llm

    async def dying_provider(feature, system, messages, tools, max_tokens):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "_chat_anthropic", dying_provider)
    client.post("/api/auth/register", json=ADMIN)

    with _pytest.raises(RuntimeError):
        asyncio.run(llm.chat(feature="agent", system="sys",
                             messages=[{"role": "user", "text": "hi"}]))

    traces = client.get("/api/admin/traces").json()["traces"]
    assert len(traces) == 1
    assert "provider down" in traces[0]["error"]


def test_trace_sanitizer_redacts_images():
    from app.services.llm import _sanitize_messages
    out = _sanitize_messages([
        {"role": "user", "text": "log this", "images": [{"media_type": "image/jpeg", "data_b64": "x" * 5000}]},
        {"role": "assistant", "raw": object()},
        {"role": "tool", "results": [{"id": "t", "name": "log_food", "content": "y" * 9000}]},
    ])
    assert out[0]["images"] == ["[image/jpeg, 5000 b64 chars]"]
    assert out[1]["raw"] == "[assistant turn]"
    assert len(out[2]["results"][0]["content"]) == 2000   # capped


# ── Server errors ─────────────────────────────────────────────────────────────

def test_unhandled_error_recorded(client, monkeypatch):
    import pytest as _pytest
    client.post("/api/auth/register", json=ADMIN)

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("app.routers.log.log_entry_for_user", boom)
    with _pytest.raises(RuntimeError):
        client.post("/api/log/", json={"food_id": 1, "quantity_g": 50})

    errors = client.get("/api/admin/errors").json()["errors"]
    assert len(errors) == 1
    assert errors[0]["path"] == "/api/log/"
    assert "kaboom" in errors[0]["error"]


def test_purge_old_telemetry(client):
    from app.database import get_conn, purge_old_telemetry
    with get_conn() as conn:
        conn.execute("""INSERT INTO model_traces (feature, provider, model, created_at)
                        VALUES ('agent','anthropic','m', datetime('now','-40 days'))""")
        conn.execute("""INSERT INTO model_traces (feature, provider, model)
                        VALUES ('agent','anthropic','m')""")
        conn.execute("INSERT INTO app_errors (method, path, error, created_at) VALUES ('GET','/x','e', datetime('now','-40 days'))")
    purge_old_telemetry(days=30)
    with get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM model_traces").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM app_errors").fetchone()["c"] == 0
