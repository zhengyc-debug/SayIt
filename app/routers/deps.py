"""路由共用的依赖：从请求里取当前登录用户。

所有业务路由都通过 Depends(current_user) 拿用户名，不要在路由里自己
解析 cookie —— 一处改，处处生效。
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from ..auth import current_user as _resolve


def _name(request: Request) -> str | None:
    """request.state.user 由登录门禁中间件写入，兜底再解析一次 cookie。"""
    user = getattr(request.state, "user", None) or _resolve(request)
    if not user:
        return None
    if isinstance(user, dict):
        name = user.get("username")
        return str(name) if name else None
    return str(user)


def current_user(request: Request) -> str:
    name = _name(request)
    if not name:
        raise HTTPException(status_code=401, detail="unauthorized")
    return name


def current_user_optional(request: Request) -> str | None:
    return _name(request)
