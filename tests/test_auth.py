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
