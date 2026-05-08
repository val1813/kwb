"""JWT认证：Token生成与验证"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt


class AuthManager:
    """JWT认证管理"""

    def __init__(self, secret: str, expire_hours: int = 8):
        self.secret = secret
        self.expire_hours = expire_hours

    def create_token(self, user_id: str, role_id: str, attrs: dict | None = None) -> str:
        """生成JWT token"""
        payload = {
            "user_id": user_id,
            "role_id": role_id,
            "attrs": attrs or {},
            "exp": datetime.now(timezone.utc) + timedelta(hours=self.expire_hours),
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def verify_token(self, token: str) -> dict:
        """验证并解码JWT token，返回payload"""
        return jwt.decode(token, self.secret, algorithms=["HS256"])

    def extract_from_header(self, authorization: str) -> dict:
        """从Authorization header提取并验证token"""
        if authorization.startswith("Bearer "):
            token = authorization[7:]
        else:
            token = authorization
        return self.verify_token(token)
