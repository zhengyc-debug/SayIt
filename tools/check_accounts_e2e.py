"""端到端自检：账号 / 后台 / 配置入库 / admin 初始化（不碰真实 data/）。

跑法：
    env -u PYTHONPATH .venv/Scripts/python.exe tools/check_accounts_e2e.py

用 SAYIT_DATA_DIR / SAYIT_LOG_DIR 指向临时目录，所以完全不会读写项目里的 data/。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="sayit-e2e-"))
# 必须在 import app.* 之前设置：paths.py 在 import 期就解析这两个变量
os.environ["SAYIT_DATA_DIR"] = str(TMP / "data")
os.environ["SAYIT_LOG_DIR"] = str(TMP / "log")
os.environ.pop("SAYIT_ADMIN_PASSWORD", None)
os.environ.pop("SAYIT_ADMIN_PASSWORD_FORCE", None)
os.environ.pop("SECRET_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.paths as paths  # noqa: E402
from app import auth  # noqa: E402
from app.main import app  # noqa: E402
from app.store import accounts, sqlite_conn  # noqa: E402
from app.store import settings as store  # noqa: E402

FAILS: list[str] = []


def wipe_db() -> None:
    """删掉库文件（含 -wal/-shm）。Windows 上句柄占用会让 rmtree 静默失败，
    所以要逐个删并检查结果。"""
    import gc
    import time

    gc.collect()
    for suffix in ("", "-wal", "-shm"):
        f = paths.DB_PATH.with_name(paths.DB_PATH.name + suffix)
        for attempt in range(5):
            try:
                if f.exists():
                    f.unlink()
                break
            except PermissionError:
                time.sleep(0.2)
    shutil.rmtree(paths.DATA_DIR, ignore_errors=True)


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def tables() -> set[str]:
    with sqlite_conn.LOCK, sqlite_conn._conn() as c:
        return {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


print(f"临时数据目录：{TMP}")
print(f"（paths 已解析：DATA_DIR={paths.DATA_DIR}）")

# ===========================================================================
print("\n[1] 全新空目录启动")
# ===========================================================================
with TestClient(app) as client:
    check("数据目录被创建", paths.DATA_DIR.is_dir())
    check("库文件被创建", paths.DB_PATH.is_file())
    ts = tables()
    check("accounts 表存在", "accounts" in ts, str(ts))
    check("user_settings 表存在", "user_settings" in ts, str(ts))
    check("app_meta 表存在", "app_meta" in ts, str(ts))
    check("业务表存在（sessions/turns/errors/words）",
          {"sessions", "turns", "errors", "words"} <= ts, str(ts))

    # 不迁移任何文件：data/users 目录和 users.csv 都不该被创建
    check("没有创建 data/users/ 目录", not (paths.DATA_DIR / "users").exists())
    check("没有创建 users.csv", not (paths.DATA_DIR / "users.csv").exists())
    check("没有创建 secret.key", not (paths.DATA_DIR / "secret.key").exists())
    check("开场白音频已从镜像目录补进数据目录",
          (paths.GREET_DIR / "index.json").is_file(), str(paths.GREET_DIR))

    admin = accounts.get_user("admin")
    check("admin 已自动创建且是管理员", admin is not None and admin["role"] == "admin")
    check("没设环境变量时随机生成密码", bool(admin and admin["password"]))

    # =======================================================================
    print("\n[2] 签名密钥存进库（不落文件）")
    # =======================================================================
    key1 = accounts.meta_get(accounts.META_SECRET_KEY)
    check("app_meta 里有 secret_key", bool(key1))
    check("长度够（>= 32 字符）", len(key1) >= 32, str(len(key1)))
    check("没有 data/secret.key 文件", not (paths.DATA_DIR / "secret.key").exists())

    # =======================================================================
    print("\n[3] 配置入库")
    # =======================================================================
    s = store.public_settings("admin")
    check("新用户读到默认配置（provider=doubao）", s["active"]["provider"] == "doubao")
    check("首次读取已落库一行", store._row("admin") is not None)

    store.update_settings("admin", {"active": {"language": "ja"},
                                    "providers": {"doubao": {"api_key": "sk-test-123"}}})
    s2 = store.load_settings("admin")
    check("改语言后能读回", s2["active"]["language"] == "ja", str(s2["active"]))
    check("没动过的字段仍是默认值", s2["active"]["scenario"] == "free_talk")
    check("API Key 落库", s2["providers"]["doubao"]["api_key"] == "sk-test-123")
    pub = store.public_settings("admin")
    check("给前端的配置里 Key 是掩码", pub["providers"]["doubao"]["api_key_set"] is True
          and "api_key" not in pub["providers"]["doubao"])

    store.save_settings("lisi", {"active": {"language": "en"}})
    check("两个用户互不干扰",
          store.load_settings("lisi")["active"]["language"] == "en"
          and store.load_settings("admin")["active"]["language"] == "ja")

    # =======================================================================
    print("\n[4] 未登录访问被拦")
    # =======================================================================
    r = client.get("/api/admin/users", follow_redirects=False)
    check("未登录 /api/admin/users → 401", r.status_code == 401, str(r.status_code))
    r = client.get("/admin", follow_redirects=False)
    check("未登录 /admin → 303 到登录页", r.status_code == 303
          and "/login" in r.headers.get("location", ""), str(r.status_code))
    r = client.get("/static/admin.js")
    check("admin.js 不在公开静态白名单里", r.status_code == 404, str(r.status_code))

    # =======================================================================
    print("\n[5] admin 登录 + 后台管理")
    # =======================================================================
    accounts.set_password("admin", "AdminPass123")
    r = client.post("/api/login", json={"username": "admin", "password": "AdminPass123"})
    check("admin 登录成功", r.json().get("ok") is True, r.text)
    check("登录返回 role=admin", r.json()["user"].get("role") == "admin", r.text)

    r = client.post("/api/admin/users", json={
        "username": "zhangsan", "password": "zs123456", "display_name": "张三"})
    check("新增普通用户 zhangsan", r.status_code == 200
          and r.json()["user"]["role"] == "user", r.text)
    check("重名新建被拒 → 400",
          client.post("/api/admin/users",
                      json={"username": "zhangsan", "password": "xxxxxxxx"}).status_code == 400)
    check("中文用户名被拒 → 400",
          client.post("/api/admin/users",
                      json={"username": "张三", "password": "123456"}).status_code == 400)
    check("短密码被拒 → 400",
          client.post("/api/admin/users",
                      json={"username": "wangwu", "password": "123"}).status_code == 400)

    users = client.get("/api/admin/users").json()["users"]
    check("列表含 admin/zhangsan", {u["username"] for u in users} >= {"admin", "zhangsan"},
          str(users))
    check("列表不含密码字段", all("password" not in u for u in users))

    # =======================================================================
    print("\n[6] 最后一个 admin 保护")
    # =======================================================================
    check("不能删自己（唯一 admin）→ 400",
          client.delete("/api/admin/users/admin").status_code == 400)
    check("不能把唯一 admin 降级 → 400",
          client.patch("/api/admin/users/admin", json={"role": "user"}).status_code == 400)
    check("不能禁用当前登录账号 → 400",
          client.patch("/api/admin/users/admin", json={"is_active": False}).status_code == 400)

    # =======================================================================
    print("\n[7] 禁用 / 启用 / 改密")
    # =======================================================================
    r = client.patch("/api/admin/users/zhangsan", json={"is_active": False})
    check("禁用 zhangsan", r.json()["user"]["is_active"] is False, r.text)
    check("禁用后登录失败",
          client.post("/api/login",
                      json={"username": "zhangsan", "password": "zs123456"}).json()["ok"] is False)
    client.patch("/api/admin/users/zhangsan", json={"is_active": True})
    check("启用后登录成功",
          client.post("/api/login",
                      json={"username": "zhangsan", "password": "zs123456"}).json()["ok"] is True)
    # 上面这次登录把会话切成了 zhangsan，改密码得先切回 admin
    client.post("/api/logout")
    client.post("/api/login", json={"username": "admin", "password": "AdminPass123"})
    r = client.patch("/api/admin/users/zhangsan", json={"password": "newpass123"})
    check("admin 改 zhangsan 的密码", r.status_code == 200, r.text)
    auth.clear_fails("testclient")
    r = client.post("/api/login", json={"username": "zhangsan", "password": "newpass123"})
    check("改密后新密码可登录", r.json().get("ok") is True, r.text)
    # 此时会话是 zhangsan（普通用户），下面 [8] 正好用它验证权限边界

    # =======================================================================
    print("\n[8] 普通用户拿不到后台")
    # =======================================================================
    r = client.get("/admin", follow_redirects=False)
    check("普通用户 /admin → 303 回首页", r.status_code == 303
          and r.headers.get("location") == "/", f"{r.status_code} {r.headers.get('location')}")
    check("普通用户后台接口 → 403", client.get("/api/admin/users").status_code == 403)
    check("普通用户 auth-state role=user",
          client.get("/api/auth-state").json()["user"]["role"] == "user")

    client.post("/api/logout")
    client.post("/api/login", json={"username": "admin", "password": "AdminPass123"})
    check("管理员 /admin → 200", client.get("/admin", follow_redirects=False).status_code == 200)
    r = client.get("/admin.js")
    check("/admin.js 对管理员可读", r.status_code == 200 and "管理员权限" in r.text)
    check("删除 zhangsan", client.delete("/api/admin/users/zhangsan").status_code == 200)
    check("zhangsan 已不存在", accounts.get_user("zhangsan") is None)

# ===========================================================================
print("\n[9] 重启：签名密钥不变、admin 密码不被覆盖")
# ===========================================================================
with TestClient(app) as client:
    check("重启后 secret_key 与之前一致",
          accounts.meta_get(accounts.META_SECRET_KEY) == key1)
    check("重启后 admin 密码没被重置",
          accounts.authenticate("admin", "AdminPass123") is not None)
    check("重启后配置还在",
          store.load_settings("admin")["active"]["language"] == "ja")

    os.environ["SAYIT_ADMIN_PASSWORD"] = "ShouldNotApply999"
    accounts.ensure_admin()
    check("已有 admin 时 SAYIT_ADMIN_PASSWORD 不覆盖密码",
          accounts.authenticate("admin", "ShouldNotApply999") is None)

# ===========================================================================
print("\n[10] 空库 + 环境变量指定密码（首次启动的正式路径）")
# ===========================================================================
del os.environ["SAYIT_ADMIN_PASSWORD"]
os.environ.pop("SAYIT_ADMIN_PASSWORD_FORCE", None)   # 第 9 步可能残留
wipe_db()
os.environ["SAYIT_ADMIN_PASSWORD"] = "FromComposeEnv2026"
auth.clear_fails("testclient")       # 清掉前面测限速留下的记录
accounts.init_accounts()
check("用 SAYIT_ADMIN_PASSWORD 建出 admin",
      accounts.authenticate("admin", "FromComposeEnv2026") is not None,
      f"admin={accounts.get_user('admin') and len(accounts.get_user('admin')['password'])} "
      f"env={os.environ.get('SAYIT_ADMIN_PASSWORD')}")
u = accounts.get_user("admin")
check("密码字段是 pbkdf2_sha256 格式", u["password"].startswith("pbkdf2_sha256$"), u["password"][:20])

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("全部通过")
