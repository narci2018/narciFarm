"""认证与授权: PBKDF2 密码哈希 + token 会话 + FastAPI 依赖。"""
import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request

from . import db

SESSION_TTL = 7 * 24 * 3600  # 7 天


def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return dk.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    dk, _ = hash_password(password, salt)
    return hmac.compare_digest(dk, password_hash)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.create_session(token, user_id, time.time() + SESSION_TTL)
    return token


def destroy_session(token: str) -> None:
    db.delete_session(token)


def user_from_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    sess = db.get_session(token)
    if not sess:
        return None
    return db.get_user(sess["user_id"])


def extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.query_params.get("token") or None


def require_user(request: Request) -> dict:
    user = user_from_token(extract_token(request))
    if not user:
        raise HTTPException(401, "未登录或会话已过期")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user
