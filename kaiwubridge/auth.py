"""JWT认证模块：Token生成与验证

职责：
- 生成带有用户身份和角色信息的JWT Token
- 验证请求中的Token有效性
- 从Authorization头中提取用户上下文
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

# Token默认有效期（小时）：企业场景下一个工作日足够，过期需重新登录
_DEFAULT_EXPIRE_HOURS = 8


class AuthManager:
    """JWT认证管理器

    所有API请求必须携带有效Token，Token中包含：
    - user_id: 用户唯一标识
    - role_id: 角色ID（决定权限范围）
    - attrs: 用户业务属性（如region、department，用于行级过滤）
    """

    def __init__(self, secret: str, expire_hours: int = _DEFAULT_EXPIRE_HOURS):
        self._secret = secret
        self._expire_hours = expire_hours

    def create_token(self, user_id: str, role_id: str, attrs: dict | None = None) -> str:
        """生成JWT Token

        Args:
            user_id: 用户ID
            role_id: 角色ID
            attrs: 用户业务属性，用于行级过滤中的占位符替换
        """
        payload = {
            "user_id": user_id,
            "role_id": role_id,
            "attrs": attrs or {},
            "exp": datetime.now(timezone.utc) + timedelta(hours=self._expire_hours),
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, self._secret, algorithm="HS256")

    def verify_token(self, token: str) -> dict:
        """验证并解码JWT Token

        Raises:
            jwt.ExpiredSignatureError: Token已过期
            jwt.InvalidTokenError: Token无效
        """
        return jwt.decode(token, self._secret, algorithms=["HS256"])

    def extract_from_header(self, authorization: str) -> dict:
        """从Authorization请求头中提取并验证Token

        支持两种格式：
        - "Bearer <token>"
        - 直接传token字符串
        """
        if authorization.startswith("Bearer "):
            token = authorization[7:]  # 跳过"Bearer "前缀（7个字符）
        else:
            token = authorization
        return self.verify_token(token)
