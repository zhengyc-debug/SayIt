"""账号/后台的端到端自检（不碰真实 data/ —— 用临时目录）。

跑法：
    env -u PYTHONPATH .venv/Scripts/python.exe tools/check_accounts_e2e.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 必须在 import app.* 之前定好数据目录
TMP = Path(tempfile.mkdtemp(prefix="sayit-acc-"))
import app.paths as paths  # noqa: E402

paths.DATA_DIR = TMP
paths.USERS_DIR = TMP / "users"
paths.DB_PATH = TMP / "sayit.db"
paths.SECRET_PATH = TMP / "secret.key"

# 老的 CSV 账号：用来验证「导入 + 哈希格式兼容」
(TMP / "users.csv").write_text(
    "username,password,display_name\n"
    "admin,pbkdf2_sha256$260000$9c7c2a7a71f82773$97f9d577b7930ce4ed9d7d8be66a3cdf13773ab13c13ed344dc2db55a4a6e890,测试\n"
    "legacy,plainpw,老用户\n", encoding="utf-8")

os.environ["SAYIT_ADMIN_PASSWORD"] = ""          # 先不设，验证随机生成分支
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.store import accounts  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


print(f"临时数据目录：{TMP}")

# ---- 1. 启动：建表 + 保证 admin ----
with TestClient(app) as client:
    check("老 CSV 里的 legacy 账号已导入", accounts.get_user("legacy") is not None)
    check("老 CSV 已改名为 .migrated", not (TMP / "users.csv").exists()
          and (TMP / "users.csv.migrated").exists())
    admin = accounts.get_user("admin")
    check("admin 存在且是管理员", admin is not None and admin["role"] == "admin")
    check("环境变量没设密码时随机生成（不是空密码）", bool(admin and admin["password"]))

    # ---- 2. 未登录访问被拦 ----
    r = client.get("/api/admin/users", follow_redirects=False)
    check("未登录访问 /api/admin/users → 401", r.status_code == 401, str(r.status_code))
    r = client.get("/admin", follow_redirects=False)
    check("未登录访问 /admin → 303 到登录页", r.status_code == 303
          and "/login" in r.headers.get("location", ""), str(r.status_code))
    r = client.get("/static/admin.js")
    check("admin.js 不在公开静态白名单里", r.status_code == 404, str(r.status_code))

    # ---- 3. 用环境变量固定 admin 密码（模拟重启时设了 SAYIT_ADMIN_PASSWORD）----
    accounts.set_password("admin", "AdminPass123")
    r = client.post("/api/login", json={"username": "admin", "password": "AdminPass123"})
    check("admin 登录成功", r.json().get("ok") is True, r.text)
    check("登录返回 role=admin", r.json()["user"].get("role") == "admin", r.text)

    # ---- 4. 后台增删改 ----
    r = client.post("/api/admin/users", json={
        "username": "lisi", "password": "lisi123456", "display_name": "李四"})
    check("新增普通用户 lisi", r.status_code == 200 and r.json()["user"]["role"] == "user", r.text)
    r = client.post("/api/admin/users", json={"username": "lisi", "password": "xxxxxxxx"})
    check("重名新建被拒 → 400", r.status_code == 400, r.text)
    r = client.post("/api/admin/users", json={"username": "张三", "password": "123456"})
    check("中文用户名被拒（提示填显示名）→ 400", r.status_code == 400, r.text)
    r = client.post("/api/admin/users", json={"username": "wangwu", "password": "123"})
    check("短密码被拒 → 400", r.status_code == 400, r.text)

    r = client.get("/api/admin/users")
    names = [u["username"] for u in r.json()["users"]]
    check("列表含 admin/lisi/legacy", set(names) >= {"admin", "lisi", "legacy"}, str(names))
    check("列表不含密码字段", all("password" not in u for u in r.json()["users"]))

    # ---- 5. 最后一个 admin 保护 ----
    r = client.delete("/api/admin/users/admin")
    check("删除自己是 admin 也不能删 → 400", r.status_code == 400, r.text)
    r = client.patch("/api/admin/users/admin", json={"role": "user"})
    check("把唯一管理员降级 → 400", r.status_code == 400, r.text)
    r = client.patch("/api/admin/users/admin", json={"is_active": False})
    check("禁用当前登录账号 → 400", r.status_code == 400, r.text)

    # ---- 6. 禁用/启用 ----
    r = client.patch("/api/admin/users/lisi", json={"is_active": False})
    check("禁用 lisi", r.status_code == 200 and r.json()["user"]["is_active"] is False, r.text)
    r = client.post("/api/login", json={"username": "lisi", "password": "lisi123456"})
    check("禁用后登录失败", r.json().get("ok") is False, r.text)
    r = client.patch("/api/admin/users/lisi", json={"is_active": True})
    check("重新启用 lisi", r.json()["user"]["is_active"] is True, r.text)
    r = client.post("/api/login", json={"username": "lisi", "password": "lisi123456"})
    check("启用后登录成功", r.json().get("ok") is True, r.text)

    # ---- 7. 普通用户拿不到 /admin ----
    r = client.get("/admin", follow_redirects=False)
    check("普通用户访问 /admin → 303 回首页", r.status_code == 303
          and r.headers.get("location") == "/", f"{r.status_code} {r.headers.get('location')}")
    r = client.get("/api/admin/users")
    check("普通用户访问后台接口 → 403", r.status_code == 403, str(r.status_code))
    r = client.get("/api/auth-state")
    check("普通用户 auth-state 里 role=user", r.json()["user"]["role"] == "user", r.text)

    # ---- 8. 老 CSV 的 pbkdf2 账号仍能登录（哈希格式兼容）----
    r = client.post("/api/logout")
    r = client.post("/api/login", json={"username": "admin", "password": "AdminPass123"})
    check("回到 admin 身份", r.json().get("ok") is True, r.text)
    accounts.set_password("legacy", "plainpw2")
    r = client.post("/api/login", json={"username": "legacy", "password": "plainpw2"})
    check("老账号改名后仍能登录", r.json().get("ok") is True, r.text)

    # ---- 9. 管理员入口 ----
    client.post("/api/logout")
    client.post("/api/login", json={"username": "admin", "password": "AdminPass123"})
    r = client.get("/admin", follow_redirects=False)
    check("管理员访问 /admin → 200", r.status_code == 200, str(r.status_code))
    r = client.get("/admin.js")
    check("/admin.js 对管理员可读", r.status_code == 200 and "管理员权限" in r.text,
          str(r.status_code))
    r = client.get("/api/auth-state")
    check("auth-state 对 admin 返回 role=admin", r.json()["user"].get("role") == "admin", r.text)

    # ---- 10. 删除 ----
    r = client.post("/api/login", json={"username": "admin", "password": "AdminPass123"})
    r = client.delete("/api/admin/users/legacy")
    check("删除 legacy", r.status_code == 200, r.text)
    check("legacy 已不存在", accounts.get_user("legacy") is None)

# ---- 11. 重启：已有 admin 不改密码；SAYIT_ADMIN_PASSWORD_FORCE 才改 ----
before = accounts.get_user("admin")["password"]
os.environ["SAYIT_ADMIN_PASSWORD"] = "NewPassFromEnv999"
accounts.ensure_admin()
check("重启且已有 admin：密码不被环境变量覆盖", accounts.get_user("admin")["password"] == before)
check("新密码此时还登不上", accounts.authenticate("admin", "NewPassFromEnv999") is None)

os.environ["SAYIT_ADMIN_PASSWORD_FORCE"] = "1"
accounts.ensure_admin()
check("FORCE=1 时按环境变量重置密码",
      accounts.authenticate("admin", "NewPassFromEnv999") is not None)

# ---- 12. 空库 + 环境变量：用环境变量建 admin ----
del os.environ["SAYIT_ADMIN_PASSWORD_FORCE"]
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
accounts.init_accounts()
check("空库首次启动用 SAYIT_ADMIN_PASSWORD 建 admin",
      accounts.authenticate("admin", "NewPassFromEnv999") is not None)

# ---- 13. 空库 + 无环境变量：随机密码 ----
del os.environ["SAYIT_ADMIN_PASSWORD"]
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
accounts.init_accounts()
u = accounts.get_user("admin")
check("空库无环境变量也能建出 admin（随机密码）", u is not None and u["role"] == "admin")
check("随机密码不能被空密码登录", accounts.authenticate("admin", "") is None)

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print(f"失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("全部通过")
