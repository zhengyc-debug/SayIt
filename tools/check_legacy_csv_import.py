"""老 users.csv → 库 的迁移验收（真跑，不 mock）。

考点：
  1. 全新库 + data/users.csv（带 BOM、明文与老哈希混合、有表头/空行/注释） → 全部入库
  2. 库里已有的用户名不覆盖密码
  3. 导入后源文件改名 .migrated 并追加导入记录
  4. 再启动一次不会重复导入、不会重复追加
"""
from __future__ import annotations

import os
import sys
import tempfile
import hashlib
from pathlib import Path

os.environ.setdefault("SAYIT_ADMIN_PASSWORD", "adminpw123")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paths  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sayit_migrate_"))
paths.DATA_DIR = TMP
paths.DB_PATH = TMP / "sayit.db"

from app.store import accounts  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{extra}]" if extra and not ok else ""))
    if not ok:
        fails.append(name)


def old_hash(pw: str, salt: str = "9c7c2a7a71f82773") -> str:
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 260_000)
    return f"pbkdf2_sha256$260000${salt}${dk.hex()}"


csv_path = TMP / "users.csv"
csv_path.write_text(
    "\ufeffusername,password,display_name\r\n"
    "alice,plainpw,爱丽丝\r\n"
    f"bob,{old_hash('bobpw')},Bob\r\n"
    "\r\n"
    "# 这一行是注释\r\n"
    "carol,short,   \r\n"
    "bad name!,x,非法用户名\r\n",
    encoding="utf-8", newline="",
)

print(f"临时数据目录：{TMP}")

print("\n[1] 第一次启动：导入")
accounts.init_accounts()

check("alice 入库（明文原样搬）", (accounts.get_user("alice") or {}).get("password") == "plainpw")
check("alice 显示名保留", (accounts.get_user("alice") or {}).get("display_name") == "爱丽丝")
check("bob 老哈希原样搬", (accounts.get_user("bob") or {}).get("password") == old_hash("bobpw"))
check("carol 空显示名回落为用户名",
      (accounts.get_user("carol") or {}).get("display_name") == "carol")
check("非法用户名没入库", accounts.get_user("bad name!") is None)
check("导入的账号是 user 角色", (accounts.get_user("alice") or {}).get("role") == "user")
check("admin 照常创建", (accounts.get_user("admin") or {}).get("role") == "admin")

print("\n[2] 导入后能登录")
check("alice 明文密码可登录", accounts.authenticate("alice", "plainpw") is not None)
check("bob 老哈希密码可登录", accounts.authenticate("bob", "bobpw") is not None)
check("错误密码被拒", accounts.authenticate("alice", "wrong") is None)

print("\n[3] 源文件改名 + 导入记录")
migrated = TMP / "users.csv.migrated"
check("users.csv 已被改名", not csv_path.exists(), str(list(TMP.iterdir())))
check("users.csv.migrated 存在", migrated.is_file())
text = migrated.read_text(encoding="utf-8")
check("末尾追加了 imported 记录", "# imported " in text and "alice" in text)
check("原内容仍在（密码原文没丢）", "alice,plainpw" in text)

print("\n[4] 再启动：不重复导入、不覆盖已改的密码")
accounts.set_password("alice", "newpw123")           # 用户在后台改了密码
before = migrated.read_text(encoding="utf-8")
rep = accounts.migrate_legacy_csv()
check("第二次没有新建任何账号", rep["created"] == [], str(rep))
check("已存在的三个账号被跳过", set(rep["skipped"]) == {"alice", "bob", "carol"}, str(rep))
check("改过的密码没被覆盖", accounts.authenticate("alice", "newpw123") is not None)
check("旧密码已失效", accounts.authenticate("alice", "plainpw") is None)
check("迁移文件没被重复追加", migrated.read_text(encoding="utf-8") == before)

print("\n[5] 没有 CSV 的机器上不报错")
migrated.unlink()
rep = accounts.migrate_legacy_csv()
check("无文件时返回空报告", rep == {"created": [], "skipped": [], "invalid": [], "source": None}, str(rep))

print("\n[6] dry-run 不写库不改文件")
csv_path.write_text("dave,davepw,Dave\n", encoding="utf-8")
rep = accounts.migrate_legacy_csv(dry_run=True)
check("dry-run 报告会新建 dave", rep["created"] == ["dave"], str(rep))
check("dry-run 后库里没有 dave", accounts.get_user("dave") is None)
check("dry-run 后源文件没被改名", csv_path.is_file())

print("\n===== " + ("全部通过" if not fails else f"{len(fails)} 项失败: {fails}") + " =====")
sys.exit(0 if not fails else 1)
