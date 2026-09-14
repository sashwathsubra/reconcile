from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app import db

SECRET_KEY = os.getenv("SECRET_KEY", "reconcile-secret-key-for-local-development-32-chars-long")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))

# 30 days for persistent session cookie
SESSION_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
COOKIE_NAME = "reconcile_session"

GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8000/auth/google/callback"
)

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def _hash_token(token: str) -> str:
    """SHA-256 hash of the JWT for storage — token itself is never stored in the DB."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(UTC) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_session(user_id: int, token: str) -> None:
    """Persist a hash of the token in the sessions table so it can be revoked."""
    token_hash = _hash_token(token)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, created_at) VALUES (%s, %s, %s)",
            (user_id, token_hash, db.now()),
        )


def revoke_session(token: str) -> None:
    """Mark the session for this token as revoked."""
    token_hash = _hash_token(token)
    with db.connection() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
            (db.now(), token_hash),
        )


def is_session_revoked(token: str) -> bool:
    """Return True if the token has been explicitly revoked."""
    token_hash = _hash_token(token)
    row = db.one("SELECT revoked_at FROM sessions WHERE token_hash = %s", (token_hash,))
    if row is None:
        # Session not found at all — treat as invalid (might be a token from before we
        # added this table, or a forged token that never got persisted).
        return True
    return row["revoked_at"] is not None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    reconcile_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """Accept the token from either the Authorization header (API / Streamlit backend calls)
    or the reconcile_session HttpOnly cookie (browser-native requests such as the OAuth
    callback redirect). Both paths hit the same revocation check.
    """
    if os.getenv("REQUIRE_LOGIN", "false").lower() == "false":
        admin_email = os.getenv("ADMIN_EMAIL", "admin@example.com").strip()
        user = db.one("SELECT id, email FROM users WHERE email = %s", (admin_email,))
        if user:
            return {"id": user["id"], "email": user["email"], "_token": "dev_token"}
        return {"id": 1, "email": admin_email, "_token": "dev_token"}

    token: str | None = None

    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    elif reconcile_session:
        token = reconcile_session

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_raw = payload.get("user_id") or payload.get("sub")
        if user_id_raw is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            user_id = int(user_id_raw)
            user = db.one("SELECT id, email, created_at FROM users WHERE id = %s", (user_id,))
        except ValueError:
            user = db.one("SELECT id, email, created_at FROM users WHERE email = %s", (str(user_id_raw),))

        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # ── Server-side revocation check ────────────────────────────────────
        if is_session_revoked(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been revoked. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return {"id": user["id"], "email": user["email"], "_token": token}

    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Could not validate credentials: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def make_session_cookie_kwargs(is_local: bool = True) -> dict[str, Any]:
    """Return kwargs suitable for `response.set_cookie()`.

    Local dev: Secure=False (no TLS), SameSite=Lax.
    Production: Secure=True, SameSite=Lax.
    SameSite=Lax (not Strict) is required so the Google OAuth redirect — which is a
    cross-site top-level navigation — still delivers the cookie on arrival.
    """
    kwargs: dict[str, Any] = dict(
        key=COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=not is_local,
        max_age=SESSION_COOKIE_MAX_AGE,
        path="/",
    )
    if cookie_domain := os.getenv("COOKIE_DOMAIN"):
        kwargs["domain"] = cookie_domain
    return kwargs
