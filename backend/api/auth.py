"""Session authentication for the admin UI."""
import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException, Request, Response

from backend import config
from backend.database import db

COOKIE = "nodepool_session"
PBKDF2_ROUNDS = 240000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2${_b64(salt)}${_b64(digest)}"


def verify_password(password: str) -> bool:
    stored = db.get("admin_password_hash")
    if stored:
        try:
            _, salt, digest = stored.split("$")
            expected = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), PBKDF2_ROUNDS)
            return hmac.compare_digest(_b64(expected), digest)
        except (ValueError, binascii.Error):
            return False
    return hmac.compare_digest(password, config.ADMIN_PASSWORD)


def make_token(hours: int | None = None) -> str:
    exp = int(time.time()) + 3600 * (hours or config.SESSION_HOURS)
    payload = _b64(json.dumps({"u": "admin", "exp": exp}).encode())
    sig = _b64(hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def check_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    expected = _b64(hmac.new(config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(_unb64(payload))
    except (ValueError, binascii.Error):
        return False
    return data.get("exp", 0) > time.time()


def set_session(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=3600 * config.SESSION_HOURS, httponly=True,
                        samesite="lax", path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


async def require_auth(request: Request) -> None:
    token = request.cookies.get(COOKIE) or request.headers.get("X-Nodepool-Token")
    if not check_token(token):
        raise HTTPException(status_code=401, detail="authentication required")


def sub_token() -> str:
    return db.get("sub_token", "") or ""
