"""账号与登录态。

账号来源：SQLite 的 accounts 表（见 app/store/accounts.py），可通过 /admin
后台或命令行管理。服务每次启动都会保证有一个 admin 账号。

登录态：签名 cookie（Starlette SessionMiddleware 实现）。
  cookie 里只放 username；签名密钥优先读环境变量 SECRET_KEY，
  没有就随机生成并**存进数据库**（app_meta 表）—— 重启不会掉登录，
  也不需要 data/secret.key 这个文件。

安全取舍（个人项目，够用为止）：
  - 密码存 pbkdf2_sha256（兼容老 CSV 里手写的明文/哈希）；
  - 用 hmac.compare_digest 做常数时间比较，避免时间侧信道；
  - 登录失败做简单的按 IP 计数限速，防止公网上被暴力猜密码；
  - 每个请求都查一次库：账号被禁用/删除后，旧 cookie 立即失效。
"""
from __future__ import annotations

import logging
import os
import secrets
import time

from fastapi import Request

from .store import accounts
from .store.accounts import META_SECRET_KEY, meta_get, meta_set

log = logging.getLogger("sayit.auth")

# 延迟取签名密钥：SessionMiddleware 在 import 期装配，但密钥要等建表后才在库里。
def lazy_secret() -> str:
    return session_secret()

SESSION_KEY = "sayit_user"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30          # 30 天，自用够长，免得反复登录

# 登录失败限速：同一 IP 在 WINDOW 秒内失败 MAX_FAILS 次就锁住
MAX_FAILS = 8
WINDOW = 300

_fails: dict[str, list[float]] = {}


# --------------------------------------------------------------------------
# 签名密钥
# --------------------------------------------------------------------------
def session_secret() -> str:
    """签名密钥优先读环境变量 SECRET_KEY；没有就用库里存着的，再没有就生成一个存进库。

    存库而不是存 data/secret.key 的原因：整个部署只剩一个 sayit.db 需要备份，
    不会出现「只恢复 db 忘了 secret.key，所有人都被踢下线」这种情况。
    """
    key = (os.environ.get("SECRET_KEY") or "").strip()
    if key:
        return key
    stored = meta_get(META_SECRET_KEY)
    if stored:
        return stored
    new_key = secrets.token_urlsafe(48)
    meta_set(META_SECRET_KEY, new_key)
    log.info("已生成登录签名密钥并存入数据库（app_meta.%s）", META_SECRET_KEY)
    return new_key


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """给命令行用的便捷入口：python -m app.auth hash 你的密码"""
    return accounts.hash_password(password)


def check_login(username: str, password: str) -> dict | None:
    """校验账号密码。成功返回用户信息（含 role），失败/被禁用返回 None。"""
    return accounts.authenticate((username or "").strip(), password or "")


def is_admin(user: dict | None) -> bool:
    return bool(user and user.get("role") == accounts.ROLE_ADMIN)


# --------------------------------------------------------------------------
# 失败限速
# --------------------------------------------------------------------------
def _now() -> float:
    return time.time()


def is_locked(ip: str) -> bool:
    hits = [t for t in _fails.get(ip, []) if _now() - t < WINDOW]
    _fails[ip] = hits
    return len(hits) >= MAX_FAILS


def record_fail(ip: str) -> None:
    _fails.setdefault(ip, []).append(_now())


def clear_fails(ip: str) -> None:
    _fails.pop(ip, None)


def lock_remaining(ip: str) -> int:
    """还要锁多少秒。"""
    hits = [t for t in _fails.get(ip, []) if _now() - t < WINDOW]
    if len(hits) < MAX_FAILS:
        return 0
    return max(0, int(WINDOW - (_now() - hits[0])))


# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
def _resolve(username: str | None) -> dict | None:
    """cookie 里的用户名 → 当前用户。账号被删/禁用就当没登录。"""
    if not username:
        return None
    return accounts.get_user_public(username)


def current_user(request: Request) -> dict | None:
    """从签名 cookie 里取当前用户；不存在或用户已被删/禁用则返回 None。"""
    username = (request.session or {}).get(SESSION_KEY)
    if not username:
        return None
    user = _resolve(username)
    if user is None:
        # 库里把这个人删了 / 禁用了，旧 cookie 立即失效
        request.session.clear()
        return None
    return user


def user_from_websocket(ws) -> dict | None:
    """WebSocket 握手同样带 cookie，复用同一套判定。"""
    return _resolve((ws.session or {}).get(SESSION_KEY))


def login_session(request: Request, user: dict) -> None:
    request.session.clear()
    request.session[SESSION_KEY] = user["username"]


def logout_session(request: Request) -> None:
    request.session.clear()


if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        print("用法: python -m app.auth hash <密码>")
