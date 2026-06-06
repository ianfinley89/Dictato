import io
import pytest

REG = {"email": "photo@example.com", "password": "password123", "display_name": "P"}

# 1x1 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _img_file(data=_PNG, mime="image/png"):
    return {"image": ("meal.png", io.BytesIO(data), mime)}


@pytest.fixture()
def _mock_vision(monkeypatch):
    """Stub the Haiku vision call and force the API key on."""
    from app.routers import photo
    from app.services import ai

    async def fake_parse_image(data, mime, uid):
        return {
            "items": [{"name": "scrambled eggs", "est_quantity_g": 100.0, "unit": "g", "confidence": 0.8}],
            "summary": "I see scrambled eggs, about one serving.",
        }

    monkeypatch.setattr(ai, "parse_image", fake_parse_image)
    monkeypatch.setattr(photo, "ANTHROPIC_API_KEY", "test-key")
    return fake_parse_image


def test_photo_requires_auth(client):
    r = client.post("/api/photo/parse", files=_img_file())
    assert r.status_code == 401


def test_photo_rejects_non_image(client, _mock_vision):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/photo/parse", files={"image": ("note.txt", io.BytesIO(b"hi"), "text/plain")})
    assert r.status_code == 415


def test_photo_returns_items(client, _mock_vision):
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/photo/parse", files=_img_file())
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "haiku"
    assert data["items"][0]["name"] == "scrambled eggs"
    assert data["summary"].startswith("I see scrambled eggs")


def test_photo_without_api_key_503(client, monkeypatch):
    from app.routers import photo
    monkeypatch.setattr(photo, "ANTHROPIC_API_KEY", "")
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/photo/parse", files=_img_file())
    assert r.status_code == 503


def test_photo_rate_limited(client, _mock_vision, monkeypatch):
    monkeypatch.setenv("AI_DAILY_LIMIT", "0")
    client.post("/api/auth/register", json=REG)
    r = client.post("/api/photo/parse", files=_img_file())
    assert r.status_code == 429


def test_photo_counts_against_usage(client, _mock_vision):
    client.post("/api/auth/register", json=REG)
    client.post("/api/photo/parse", files=_img_file())
    usage = client.get("/api/voice/usage").json()
    assert usage["vision_calls"] == 1
