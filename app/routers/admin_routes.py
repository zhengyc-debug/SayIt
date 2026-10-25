"""后台管理：人员管理（仅 admin）。

设计要点：
  · 权限判定就一处 —— _admin()。所有接口第一行都过它，不在各 handler 里散落判断。
  · 删人/禁用自己不拦在 UI，拦在接口：disable/delete/降级最后一个 admin 都会被拒，
    否则一次误点就把所有人锁在门外。
  · 这里不提供「注册」接口：账号只能由 admin 建，符合自用 + 小团队场景。

接口列表：
  GET    /api/admin/users            人员列表
  POST   /api/admin/users            新增（username, password, display_name, role）
  PATCH  /api/admin/users/{name}     改显示名 / 角色 / 密码 / 启用状态
  DELETE /api/admin/users/{name}     删除
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import auth
from ..store import accounts

log = logging.getLogger("sayit.admin_routes")

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin(request: Request) -> dict:
    user = auth.current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _last_admin_guard(target: str, *, new_role: str | None = None,
                      new_active: bool | None = None, deleting: bool = False) -> None:
    """防止把最后一个可用 admin 干掉 / 降权 / 禁用。"""
    target_user = accounts.get_user(target)
    if target_user is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if target_user.get("role") != accounts.ROLE_ADMIN or not target_user.get("is_active"):
        return
    losing = (deleting
              or (new_role is not None and new_role != accounts.ROLE_ADMIN)
              or new_active is False)
    if losing and accounts.count_active_admins() <= 1:
        raise HTTPException(status_code=400, detail="这是最后一个可用的管理员账号，不能删除/降级/禁用")


@router.get("/users")
async def list_users(request: Request) -> dict[str, Any]:
    me = _admin(request)
    return {"ok": True, "me": me["username"], "users": accounts.list_users()}


@router.post("/users")
async def create_user(request: Request) -> dict[str, Any]:
    _admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    try:
        user = accounts.create_user(
            str(body.get("username") or ""),
            str(body.get("password") or ""),
            str(body.get("display_name") or ""),
            str(body.get("role") or accounts.ROLE_USER),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info("admin 新建账号 %s", user["username"])
    return {"ok": True, "user": user}


@router.patch("/users/{username}")
async def update_user(username: str, request: Request) -> dict[str, Any]:
    me = _admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    if accounts.get_user(username) is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    role = body.get("role")
    active = body.get("is_active")
    if role is not None:
        role = str(role)
        if role not in (accounts.ROLE_ADMIN, accounts.ROLE_USER):
            raise HTTPException(status_code=400, detail="role 只能是 admin 或 user")
    if active is not None:
        active = bool(active)

    # 自己禁用自己 = 立刻掉线，明确拒掉比让人猜为什么被踢出更友好
    if username == me["username"] and active is False:
        raise HTTPException(status_code=400, detail="不能禁用当前登录的账号")
    _last_admin_guard(username, new_role=role, new_active=active)

    if role is not None:
        accounts.set_role(username, role)
    if active is not None:
        accounts.set_active(username, active)
    if body.get("display_name") is not None:
        accounts.set_display_name(username, str(body.get("display_name") or ""))
    if body.get("password"):
        try:
            accounts.set_password(username, str(body["password"]))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    log.info("admin %s 修改了账号 %s", me["username"], username)
    return {"ok": True, "user": accounts.get_user_public(username)}


@router.delete("/users/{username}")
async def delete_user(username: str, request: Request) -> dict[str, Any]:
    me = _admin(request)
    if username == me["username"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录的账号")
    _last_admin_guard(username, deleting=True)
    if not accounts.delete_user(username):
        raise HTTPException(status_code=404, detail="账号不存在")
    log.warning("admin %s 删除了账号 %s", me["username"], username)
    return {"ok": True}
