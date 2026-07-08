REG = {"email": "alice@example.com", "password": "password123", "display_name": "Alice"}


def test_register_returns_user(client):
    r = client.post("/api/auth/register", json=REG)
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice"


def test_register_sets_session_cookie(client):
    r = client.post("/api/auth/register", json=REG)
    assert "dictato_session" in r.cookies


def test_duplicate_email_rejected(client):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/auth/register", json=REG)
    assert r.status_code == 400


def test_login_valid(client):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/auth/login", json={"email": REG["email"], "password": REG["password"]})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Alice"


def test_login_wrong_password(client):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/auth/login", json={"email": REG["email"], "password": "wrong"})
    assert r.status_code == 401


def test_me_requires_auth(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_returns_user(client):
    client.post("/api/auth/register", json=REG)
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["email"] == REG["email"]


def test_logout_clears_session(client):
    client.post("/api/auth/register", json=REG)
    client.post("/api/auth/logout", json={})
    r = client.get("/api/auth/me")
    assert r.status_code == 401


# ── Account deletion ──────────────────────────────────────────────────────────

def _seed_user_data(client):
    """Give the account a food, a log entry, and a favorite to verify the wipe."""
    import json as _json
    from app.database import get_conn
    with get_conn() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email=?", (REG["email"],)).fetchone()["id"]
        nutrients = _json.dumps({"calories": 100, "protein_g": 5, "carbs_g": 10, "fat_g": 2, "fiber_g": 1})
        fid = conn.execute(
            "INSERT INTO foods (source, name, nutrients_json, created_by_user_id) VALUES ('user','my snack',?,?)",
            (nutrients, uid),
        ).lastrowid
    client.post("/api/log/", json={"food_id": fid, "quantity_g": 50})
    client.post(f"/api/foods/{fid}/favorite", json={})
    return uid, fid


def test_delete_account_requires_correct_password(client):
    client.post("/api/auth/register", json=REG)
    r = client.request("DELETE", "/api/auth/account", json={"password": "wrong-password"})
    assert r.status_code == 403
    assert client.get("/api/auth/me").status_code == 200   # still logged in, nothing deleted


def test_delete_account_wipes_data_and_session(client):
    client.post("/api/auth/register", json=REG)
    uid, fid = _seed_user_data(client)

    r = client.request("DELETE", "/api/auth/account", json={"password": REG["password"]})
    assert r.status_code == 200

    from app.database import get_conn
    with get_conn() as conn:
        assert conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone() is None
        assert conn.execute("SELECT id FROM log_entries WHERE user_id=?", (uid,)).fetchone() is None
        assert conn.execute("SELECT id FROM favorites WHERE user_id=?", (uid,)).fetchone() is None
        assert conn.execute("SELECT id FROM foods WHERE id=?", (fid,)).fetchone() is None

    assert client.get("/api/auth/me").status_code == 401   # session cleared


def test_delete_account_keeps_public_cache_foods(client):
    import json as _json
    from app.database import get_conn
    client.post("/api/auth/register", json=REG)
    with get_conn() as conn:
        uid = conn.execute("SELECT id FROM users WHERE email=?", (REG["email"],)).fetchone()["id"]
        nutrients = _json.dumps({"calories": 380, "protein_g": 8, "carbs_g": 80, "fat_g": 3, "fiber_g": 3})
        pub = conn.execute(
            "INSERT INTO foods (source, name, nutrients_json) VALUES ('usda','rice cake',?)",
            (nutrients,),
        ).lastrowid
        web = conn.execute(
            "INSERT INTO foods (source, name, nutrients_json, created_by_user_id) VALUES ('web','burrito',?,?)",
            (nutrients, uid),
        ).lastrowid

    client.request("DELETE", "/api/auth/account", json={"password": REG["password"]})

    with get_conn() as conn:
        assert conn.execute("SELECT id FROM foods WHERE id=?", (pub,)).fetchone() is not None
        # 'web' foods are shared cache other users may have logged — kept
        assert conn.execute("SELECT id FROM foods WHERE id=?", (web,)).fetchone() is not None
