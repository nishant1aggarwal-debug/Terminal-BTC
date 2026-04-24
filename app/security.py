"""HTTP basic auth dependency for the dashboard and control endpoints.

Zero-config open mode by default (dev-friendly). When both ``DASHBOARD_USER``
and ``DASHBOARD_PASS`` are set in env, every protected route requires a
matching ``Authorization: Basic <base64>`` header or returns 401 with
``WWW-Authenticate: Basic realm="terminal-btc"``.

Healthchecks stay public so Render/Kubernetes/etc can probe them.
"""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from app.config import get_settings


async def require_basic_auth(request: Request) -> None:
    """FastAPI dependency: 401 if credentials don't match env.

    When env is not configured, this is a no-op (open mode). When configured,
    it parses the Authorization header and constant-time-compares user+pass.
    """
    settings = get_settings()
    expected_user = settings.dashboard_user
    expected_pass = settings.dashboard_pass

    # Open mode — neither var set, skip entirely.
    if not expected_user and not expected_pass:
        return

    # Partial config is a misconfiguration, not a backdoor. Treat as closed.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("basic "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Basic auth required",
            headers={"WWW-Authenticate": 'Basic realm="terminal-btc"'},
        )

    import base64
    try:
        decoded = base64.b64decode(auth_header[6:], validate=True).decode("utf-8", errors="strict")
        user, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed basic auth",
            headers={"WWW-Authenticate": 'Basic realm="terminal-btc"'},
        )

    # Constant-time compare to prevent timing attacks on the password.
    user_ok = secrets.compare_digest(user.encode(), expected_user.encode())
    pass_ok = secrets.compare_digest(password.encode(), expected_pass.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="terminal-btc"'},
        )
