"""HTTP basic auth tests — open mode + closed mode around /healthz, /api, /ui."""
from __future__ import annotations

import base64
import os

import pytest
from fastapi.testclient import TestClient


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _build_app(monkeypatch, user: str | None, password: str | None):
    # Each test needs a fresh app + settings cache so env changes actually apply.
    from app.config import get_settings
    if user is None:
        monkeypatch.delenv("DASHBOARD_USER", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_USER", user)
    if password is None:
        monkeypatch.delenv("DASHBOARD_PASS", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_PASS", password)
    # Also isolate the DB so the lifespan doesn't trip on real data.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    get_settings.cache_clear()
    # Re-import app fresh each time — FastAPI caches the mount.
    import importlib
    import app.main
    importlib.reload(app.main)
    return app.main.app


def test_open_mode_allows_everything(monkeypatch):
    app = _build_app(monkeypatch, user=None, password=None)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/overview").status_code == 200
        # /ui/ redirect or 200 are both fine; we care that it's not 401.
        assert client.get("/ui/", follow_redirects=False).status_code != 401


def test_closed_mode_requires_auth(monkeypatch):
    app = _build_app(monkeypatch, user="admin", password="s3cret")
    with TestClient(app) as client:
        # Healthchecks stay public even in closed mode.
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        # Protected surfaces 401 without creds.
        r = client.get("/api/overview")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")
        assert client.get("/ui/", follow_redirects=False).status_code == 401
        # Wrong password also 401.
        bad = client.get("/api/overview", headers=_basic("admin", "wrong"))
        assert bad.status_code == 401
        # Correct creds pass.
        ok = client.get("/api/overview", headers=_basic("admin", "s3cret"))
        assert ok.status_code == 200
