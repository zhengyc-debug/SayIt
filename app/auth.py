"""账号与登录态。

账号来源：data/users.csv（三列：username,password,display_name），写死即可，
单人自用不需要注册/改密流程。文件每次登录时重新读，改完不用重启服务。

登录态：签名 cookie（Starlette SessionMiddleware 实现）。
  cookie 里只放 username，签名密钥从 .env 的 SECRET_KEY 读；
  没配 SECRET_KEY 就随机生成一个，此时重启服务会让已登录的会话失效
  （只影响体验，不影响安全）。

安全取舍（个人项目，够用为止）：
  - 密码支持明文和 pbkdf2_sha256 哈希两种写法，CSV 里想放哈希就放哈希；
  - 用 hmac.compare_digest 做常数时间比较，避免时间侧信道；
  - 登录失败做简单的按 IP 计数限速，防止公网上被暴力猜密码。
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import Request

log = logging.getLogger("sayit.auth")

BASE_DIR = Path(__file__).resolve().parent.parent
USERS_CSV = BASE_DIR / "data" / "users.csv"

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
    key = (os.environ.get("SECRET_KEY") or "").strip()
    if key:
        return key
    log.warning("未设置 SECRET_KEY，已随机生成临时密钥："
                "重启服务后所有人需要重新登录。请在 .env 里设一个固定值。")
    return secrets.token_urlsafe(48)


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------
def _hash_password(password: str, salt: str) -> str:
    """生成 pbkdf2_sha256 哈希，格式 pbkdf2_sha256$迭代次数$salt$hex。"""
    iterations = 260_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def hash_password(password: str) -> str:
    """给命令行用的便捷入口：python -m app.auth hash 你的密码"""
    return _hash_password(password, secrets.token_hex(8))


def _verify_password(password: str, stored: str) -> bool:
    stored = (stored or "").strip()
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters, salt, hexdigest = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
            return hmac.compare_digest(dk.hex(), hexdigest)
        except Exception:
            log.exception("users.csv 里的哈希格式不合法")
            return False
    # 明文
    return hmac.compare_digest(password, stored)


def load_users() -> dict[str, dict[str, str]]:
    """读 data/users.csv，返回 {username: {password, display_name}}。"""
    users: dict[str, dict[str, str]] = {}
    if not USERS_CSV.exists():
        log.warning("账号文件不存在：%s —— 任何人都无法登录", USERS_CSV)
        return users
    try:
        # utf-8-sig：Windows 上用 Excel 存 CSV 会带 BOM，不这么读第一列会变成 "\ufeffusername"
        with USERS_CSV.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("username") or "").strip()
                if not name or name.startswith("#"):
                    continue
                users[name] = {
                    "password": (row.get("password") or "").strip(),
                    "display_name": (row.get("display_name") or name).strip(),
                }
    except Exception:
        log.exception("读取账号文件失败：%s", USERS_CSV)
    return users


def check_login(username: str, password: str) -> dict[str, str] | None:
    """校验账号密码。成功返回用户信息，失败返回 None。"""
    username = (username or "").strip()
    password = password or ""
    users = load_users()
    user = users.get(username)
    if user is None:
        # 账号不存在也走一次哈希比较，避免用耗时差异探测用户名是否存在
        _verify_password(password, "pbkdf2_sha256$260000$0000$" + "0" * 64)
        return None
    if not _verify_password(password, user["password"]):
        return None
    return {"username": username, "display_name": user["display_name"]}


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
def current_user(request: Request) -> dict[str, str] | None:
    """从签名 cookie 里取当前用户；不存在或用户已被删则返回 None。"""
    username = (request.session or {}).get(SESSION_KEY)
    if not username:
        return None
    user = load_users().get(username)
    if user is None:
        # CSV 里把这个人删了 / 改名了，旧 cookie 立即失效
        request.session.clear()
        return None
    return {"username": username, "display_name": user["display_name"]}


def user_from_websocket(ws) -> dict[str, str] | None:
    """WebSocket 握手同样带 cookie，复用同一套判定。"""
    username = (ws.session or {}).get(SESSION_KEY)
    if not username:
        return None
    user = load_users().get(username)
    if user is None:
        return None
    return {"username": username, "display_name": user["display_name"]}


def login_session(request: Request, user: dict[str, str]) -> None:
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
