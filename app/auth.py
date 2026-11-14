"""Optional single-password login (APP_PASSWORD). Off when empty."""
from __future__ import annotations

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, TimestampSigner
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

COOKIE = "asx_session"
MAX_AGE = 30 * 86400
OPEN_PATHS = ("/healthz", "/login", "/static/")


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().get_secret_key(), salt="asx-login")


def password_ok(given: str) -> bool:
    expected = get_settings().app_password
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def make_cookie() -> str:
    return _signer().sign(b"ok").decode()


def cookie_valid(value: str | None) -> bool:
    if not value:
        return False
    try:
        _signer().unsign(value, max_age=MAX_AGE)
        return True
    except BadSignature:
        return False


class PasswordMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not get_settings().app_password:
            return await call_next(request)
        path = request.url.path
        if path.startswith(OPEN_PATHS) or cookie_valid(request.cookies.get(COOKIE)):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)
