import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Give each test its own fresh SQLite database."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    from app.database import init_db
    init_db()
    yield


@pytest.fixture()
def client():
    """Fresh TestClient (and therefore fresh cookie jar) for each test."""
    from app.main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
