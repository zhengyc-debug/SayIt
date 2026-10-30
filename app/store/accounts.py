"""账号存储（SQLite）。

为什么从 csv 搬到库里：
  · CSV 只能手改、两个人同时改会互相覆盖，也没法给「后台加人」提供一条
    可审计的写入路径；
  · 账号本来就和通话记录同生共死（删人不删记录就没法对账），放一个库更好备份。

表 accounts：
  username      登录名，主键，区分大小写（SQLite 默认 TEXT 是 BINARY 比较）
  password      明文 或 pbkdf2_sha256$迭代$salt$hex —— 兼容老 CSV 里手写的哈希
  display_name  界面上显示的名字
  role          admin | user。admin 能进 /admin 管理别人
  created_at    首次创建时间
  is_active     0 = 禁用（保留数据，登录直接拒）

admin 账号：
  服务启动时 ensure_admin() 会保证库里一定有一个 admin：
    · 已有 → 什么都不做（绝不重置密码，否则重启就把自己锁在外面）
    · 没有且环境变量 SAYIT_ADMIN_PASSWORD 有值 → 用它建
    · 没有且环境变量没设 → 随机生成一个，打印到日志（只打印这一次）
  想重置密码：环境变量 SAYIT_ADMIN_PASSWORD_FORCE=1 再重启。

老 CSV 一次性导入（migrate_legacy_csv / 命令行 `import-csv`）：
  老版本用 data/users.csv（username,password,display_name）存账号。启动时会找
  data/users.csv 和 data/users.csv.migrated，把库里的账号补齐：
    · 库里没有的用户名 → 新建（哈希/明文原样搬进 password 列，登录照样能过，
      下次改密码时自动升级成新哈希）
    · 库里已有的用户名 → 跳过，绝不覆盖现有密码（宁可让人自己改，也不能
      因为一次自动导入把能登录的密码换掉）
  导入完把源文件改名成 users.csv.migrated 并在文件末尾追加一行注释，记录
  「导入了哪个用户、什么时候」，重复启动不会重复导入（INSERT 前都查过）。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .sqlite_conn import LOCK, _conn

log = logging.getLogger("sayit.accounts")

ADMIN_USERNAME = "admin"
ADMIN_DEFAULT_DISPLAY = "管理员"

ROLE_ADMIN = "admin"
ROLE_USER = "user"

# 存在 app_meta 里的键
META_SECRET_KEY = "secret_key"          # 登录 cookie 签名密钥

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username      TEXT PRIMARY KEY,
    password      TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'user',
    created_at    REAL NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

-- 杂项键值：签名密钥这类「只有一份」的运行时状态。
-- 放库里以便整个部署只有一个 sayit.db 需要备份。
CREATE TABLE IF NOT EXISTS app_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# 用户名规则：登录名要能当目录名（data/users/<name>/settings.json）用，
# 所以限制在字母数字和几个安全符号。中文显示名走 display_name，不要塞进用户名。
USERNAME_MAX = 32
USERNAME_ALLOWED = set("abcdefghijklmnopqrstuvwxyz"
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                       "0123456789_.@-")
PASSWORD_MIN = 6


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: str) -> str:
    iterations = 260_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def hash_password(password: str) -> str:
    """生成新哈希。命令行：python -m app.accounts hash 你的密码"""
    return _hash_password(password, secrets.token_hex(8))


def verify_password(password: str, stored: str) -> bool:
    """校验密码。支持老格式（明文）和新格式（pbkdf2_sha256）。"""
    stored = (stored or "").strip()
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iters, salt, hexdigest = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
            return hmac.compare_digest(dk.hex(), hexdigest)
        except Exception:
            log.exception("账号密码字段的哈希格式不合法")
            return False
    return hmac.compare_digest(password, stored)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate_username(username: str) -> str:
    """非法用户名抛 ValueError，合法返回去空白后的值。"""
    name = (username or "").strip()
    if not name:
        raise ValueError("用户名不能为空")
    if len(name) > USERNAME_MAX:
        raise ValueError(f"用户名最长 {USERNAME_MAX} 个字符")
    bad = [ch for ch in name if ch not in USERNAME_ALLOWED]
    if bad:
        raise ValueError("用户名只能用字母、数字和 _ . @ - （中文请填在「显示名」里）")
    if name.startswith("."):
        raise ValueError("用户名不能以点开头")
    return name


def validate_password(password: str) -> str:
    if not password or len(password) < PASSWORD_MIN:
        raise ValueError(f"密码至少 {PASSWORD_MIN} 位")
    return password


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def _public(row: Any) -> dict[str, Any]:
    return {
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"] or ROLE_USER,
        "created_at": row["created_at"],
        "is_active": bool(row["is_active"]),
    }


def get_user(username: str) -> dict[str, Any] | None:
    """按用户名取账号（含密码哈希，只给认证流程用）。用户不存在返回 None。"""
    username = (username or "").strip()
    if not username:
        return None
    with LOCK, _conn() as c:
        row = c.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    """后台人员列表。不含密码字段。"""
    with LOCK, _conn() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY"
                         " CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at").fetchall()
    return [_public(r) for r in rows]


def count_users() -> int:
    with LOCK, _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM accounts").fetchone()["n"])


def count_active_admins() -> int:
    with LOCK, _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM accounts"
                             " WHERE role=? AND is_active=1", (ROLE_ADMIN,)).fetchone()["n"])


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def create_user(username: str, password: str, display_name: str = "",
                role: str = ROLE_USER) -> dict[str, Any]:
    """新建账号。用户名已存在抛 ValueError（不静默覆盖别人的密码）。"""
    username = validate_username(username)
    validate_password(password)
    role = role if role in (ROLE_ADMIN, ROLE_USER) else ROLE_USER
    display = (display_name or "").strip() or username
    with LOCK, _conn() as c:
        if c.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
            raise ValueError(f"用户名 {username} 已存在")
        c.execute(
            "INSERT INTO accounts (username, password, display_name, role, created_at,"
            " is_active) VALUES (?,?,?,?,?,1)",
            (username, hash_password(password), display, role, time.time()))
    log.info("新建账号 user=%s role=%s", username, role)
    return get_user_public(username) or {"username": username}


def get_user_public(username: str) -> dict[str, Any] | None:
    row = get_user(username)
    return _public(row) if row else None


def set_password(username: str, password: str) -> bool:
    validate_password(password)
    with LOCK, _conn() as c:
        cur = c.execute("UPDATE accounts SET password=? WHERE username=?",
                        (hash_password(password), username))
    return (cur.rowcount or 0) > 0


def set_active(username: str, active: bool) -> bool:
    """禁用 / 启用。禁用后登录被拒，已有 cookie 也会失效（每次请求都查库）。"""
    with LOCK, _conn() as c:
        cur = c.execute("UPDATE accounts SET is_active=? WHERE username=?",
                        (1 if active else 0, username))
    return (cur.rowcount or 0) > 0


def set_display_name(username: str, display_name: str) -> bool:
    with LOCK, _conn() as c:
        cur = c.execute("UPDATE accounts SET display_name=? WHERE username=?",
                        ((display_name or "").strip() or username, username))
    return (cur.rowcount or 0) > 0


def set_role(username: str, role: str) -> bool:
    if role not in (ROLE_ADMIN, ROLE_USER):
        raise ValueError("role 只能是 admin 或 user")
    with LOCK, _conn() as c:
        cur = c.execute("UPDATE accounts SET role=? WHERE username=?", (role, username))
    return (cur.rowcount or 0) > 0


def delete_user(username: str) -> bool:
    """删账号。通话记录保留（数据是那个人自己的，删了就当孤儿行留着）。"""
    with LOCK, _conn() as c:
        cur = c.execute("DELETE FROM accounts WHERE username=?", (username,))
    return (cur.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """校验账号密码。成功返回公开字段 + role，失败/被禁返回 None。"""
    user = get_user(username)
    if user is None:
        # 账号不存在也走一次哈希比较，避免用耗时差异探测用户名是否存在
        verify_password(password or "", "pbkdf2_sha256$260000$0000$" + "0" * 64)
        return None
    if not user.get("is_active"):
        return None
    if not verify_password(password or "", user.get("password") or ""):
        return None
    return _public(user)


# ---------------------------------------------------------------------------
# 启动初始化：admin 账号 + 老 CSV 导入
# ---------------------------------------------------------------------------
def _random_password(n: int = 14) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def ensure_admin() -> None:
    """保证存在 admin。已有 admin 时什么都不做；返回是否本次新建/改密。"""
    existing = get_user(ADMIN_USERNAME)
    env_password = (os.environ.get("SAYIT_ADMIN_PASSWORD") or "").strip()
    force = (os.environ.get("SAYIT_ADMIN_PASSWORD_FORCE") or "").strip().lower() in (
        "1", "true", "yes", "on")

    if existing is not None:
        # force 且给了新密码，且确实换了密码 → 重置。
        # （force=1 一直留在环境里，但密码不改，就不会每次重启都刷一遍库）
        if force and env_password and not verify_password(
                env_password, existing.get("password") or ""):
            _apply_password_if_changed(existing, env_password)
        return

    if env_password:
        password, generated = env_password, False
    else:
        password, generated = _random_password(), True

    # 直接写库而不走 create_user：密码可能不足 6 位（老用户按习惯设的短密码），
    # 启动阶段不该因为校验规则把自己卡住。
    with LOCK, _conn() as c:
        c.execute(
            "INSERT INTO accounts (username, password, display_name, role, created_at,"
            " is_active) VALUES (?,?,?,?,?,1)",
            (ADMIN_USERNAME, hash_password(password), ADMIN_DEFAULT_DISPLAY,
             ROLE_ADMIN, time.time()))

    if generated:
        log.warning(
            "\n" + "=" * 62 +
            "\n  已创建管理员账号（没给 SAYIT_ADMIN_PASSWORD，随机生成）" +
            f"\n    用户名: {ADMIN_USERNAME}" +
            f"\n    密码:   {password}" +
            "\n  这个密码只显示这一次。想固定密码：在 docker-compose.yml 的" +
            "\n    environment 里写 SAYIT_ADMIN_PASSWORD: 你的密码" +
            "\n  然后 docker compose down -v 后重新 up（会清空数据卷）——" +
            "\n  或者直接登录后在「后台 · 人员管理」里改自己的密码。" +
            "\n" + "=" * 62)
    else:
        log.info("已用 SAYIT_ADMIN_PASSWORD 创建管理员账号 %s", ADMIN_USERNAME)


def _apply_password_if_changed(existing: dict[str, Any], env_password: str) -> None:
    if verify_password(env_password, existing.get("password") or ""):
        log.info("admin 密码与环境变量一致，跳过")
        return
    with LOCK, _conn() as c:
        c.execute("UPDATE accounts SET password=?, role=?, is_active=1 WHERE username=?",
                  (hash_password(env_password), ROLE_ADMIN, ADMIN_USERNAME))
    log.warning("已按 SAYIT_ADMIN_PASSWORD_FORCE 重置 %s 的密码", ADMIN_USERNAME)


def meta_get(key: str) -> str:
    """读 app_meta。没有或为空都返回空串。"""
    with LOCK, _conn() as c:
        row = c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return (row["value"] if row else "") or ""


def meta_set(key: str, value: str) -> None:
    with LOCK, _conn() as c:
        c.execute("INSERT INTO app_meta (key, value) VALUES (?,?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def init_accounts() -> None:
    """启动时调一次：建表 → 导入老 CSV → 保证有 admin。"""
    from .sqlite_conn import ensure_schema
    ensure_schema()
    try:
        migrate_legacy_csv()
    except Exception:  # 导入失败绝不能拦住启动（账号导不进去还能用命令行补）
        log.exception("老 users.csv 导入失败，已跳过（账号可用命令行 import-csv 再试）")
    ensure_admin()


# ---------------------------------------------------------------------------
# 老 CSV 导入
# ---------------------------------------------------------------------------
LEGACY_CSV_NAME = "users.csv"          # 老版本的账号文件
LEGACY_CSV_DONE_SUFFIX = ".migrated"   # 导入完改名成这个


def _candidate_csv_paths() -> list[Any]:
    """data/users.csv 和 data/users.csv.migrated（后者是为了一步没导全时还能补）。"""
    from .. import paths
    base = paths.DATA_DIR / LEGACY_CSV_NAME
    return [base, base.with_name(LEGACY_CSV_NAME + LEGACY_CSV_DONE_SUFFIX)]


def read_legacy_csv(path: Any) -> list[dict[str, str]]:
    """读老 CSV，返回 [{username, password, display_name}]。

    兼容 Excel 存出来的 BOM（utf-8-sig）、裸两列（没有表头）、# 注释行。
    老文件只有三列，role 一律按 user；admin 由 ensure_admin() 保证存在。
    """
    import csv

    rows: list[dict[str, str]] = []
    # utf-8-sig：Windows 上用 Excel 存 CSV 会带 BOM，不这么读第一列会变成 "\ufeffusername"
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            if not raw or not (raw[0] or "").strip():
                continue
            first = raw[0].strip()
            if first.startswith("#"):
                continue
            if first.lower() == "username":       # 表头
                continue
            name = first
            password = (raw[1] if len(raw) > 1 else "").strip()
            display = (raw[2] if len(raw) > 2 else "").strip() or name
            rows.append({"username": name, "password": password, "display_name": display})
    return rows


def migrate_legacy_csv(dry_run: bool = False) -> dict[str, Any]:
    """把老 users.csv 的账号补进库。幂等：库里已有的用户名跳过、不覆盖密码。

    返回 {"created": [...], "skipped": [...], "invalid": [...], "source": 路径或 None}。
    dry_run=True 只算不写（给命令行 --dry-run 用）。
    """
    source = next((p for p in _candidate_csv_paths() if p.is_file()), None)
    if source is None:
        return {"created": [], "skipped": [], "invalid": [], "source": None}
    return migrate_legacy_csv_path(source, dry_run=dry_run)


def migrate_legacy_csv_path(source: Any, dry_run: bool = False) -> dict[str, Any]:
    """从指定路径导入（命令行 --path 用），其余语义同 migrate_legacy_csv。"""
    report: dict[str, Any] = {"created": [], "skipped": [], "invalid": [],
                              "source": str(source)}
    if not Path(source).is_file():
        raise FileNotFoundError(f"CSV 不存在：{source}")

    for row in read_legacy_csv(source):
        name = row["username"]
        try:
            name = validate_username(name)
        except ValueError as e:
            report["invalid"].append(f"{row['username']}: {e}")
            continue

        existing = get_user(name)
        if existing is not None:
            report["skipped"].append(name)
            continue
        if dry_run:
            report["created"].append(name)
            continue

        # 老密码原样搬（明文 / 老哈希都行，verify_password 两种都认），
        # 不走 create_user：老账号可能有不足 6 位的密码，而且这里不能重新哈希 ——
        # 明文原文丢了就找不回来了。
        with LOCK, _conn() as c:
            c.execute(
                "INSERT INTO accounts (username, password, display_name, role,"
                " created_at, is_active) VALUES (?,?,?,?,?,1)",
                (name, row["password"], row["display_name"], ROLE_USER, time.time()))
        report["created"].append(name)

    if report["created"] and not dry_run:
        _mark_csv_imported(source, report["created"])
        log.info("老 users.csv 导入完成：新建 %d 个账号 %s，跳过已存在 %d 个（来源 %s）",
                 len(report["created"]), ", ".join(report["created"]),
                 len(report["skipped"]), source)
    return report


def _mark_csv_imported(source: Any, created: list[str]) -> None:
    """导入完改名成 users.csv.migrated，并在末尾追加一行导入记录。

    追加注释而不是删文件：老密码的原文/老哈希只在这一个地方有，出问题还能翻回去。
    """
    target = source
    if not str(source).endswith(LEGACY_CSV_DONE_SUFFIX):
        target = source.with_name(source.name + LEGACY_CSV_DONE_SUFFIX)
        if target.exists():
            # 已经有一份 .migrated 了（比如上次导了一半又放了新的 users.csv）：
            # 带时间戳另存，绝不覆盖上一次的备份。
            target = source.with_name(
                f"{source.name}{LEGACY_CSV_DONE_SUFFIX}.{int(time.time())}")
        source.replace(target)
    try:
        with open(target, "a", encoding="utf-8", newline="") as f:
            f.write(f"# imported {time.strftime('%Y-%m-%d %H:%M:%S')}: "
                    f"{', '.join(created)}\n")
    except OSError:
        log.warning("没能往 %s 追加导入记录（账号已入库，不影响使用）", target)



if __name__ == "__main__":  # pragma: no cover
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="SayIt 账号命令行工具")
    sub = parser.add_subparsers(dest="cmd")

    p_hash = sub.add_parser("hash", help="生成密码哈希")
    p_hash.add_argument("password")

    p_add = sub.add_parser("add", help="新建账号")
    p_add.add_argument("username")
    p_add.add_argument("password")
    p_add.add_argument("--display", default="")
    p_add.add_argument("--admin", action="store_true")

    p_pw = sub.add_parser("passwd", help="改密码")
    p_pw.add_argument("username")
    p_pw.add_argument("password")

    p_list = sub.add_parser("list", help="列出账号")

    p_imp = sub.add_parser("import-csv", help="把老 data/users.csv 导入库（已存在的跳过）")
    p_imp.add_argument("--path", default="", help="指定 CSV 路径（默认 data/users.csv）")
    p_imp.add_argument("--dry-run", action="store_true", help="只预览，不写库、不改文件")

    args = parser.parse_args()
    from .sqlite_conn import ensure_schema
    ensure_schema()

    if args.cmd == "hash":
        print(hash_password(args.password))
    elif args.cmd == "add":
        u = create_user(args.username, args.password, args.display,
                        ROLE_ADMIN if args.admin else ROLE_USER)
        print(f"已创建 {u['username']}（{u['role']}）")
    elif args.cmd == "passwd":
        print("已修改" if set_password(args.username, args.password) else "账号不存在")
    elif args.cmd == "list":
        for u in list_users():
            flag = "" if u["is_active"] else "（已禁用）"
            print(f"{u['username']:<20} {u['role']:<6} {u['display_name']}{flag}")
    elif args.cmd == "import-csv":
        if args.path:
            rep = migrate_legacy_csv_path(args.path, dry_run=args.dry_run)
        else:
            rep = migrate_legacy_csv(dry_run=args.dry_run)
        if rep["source"] is None:
            print("没找到 data/users.csv，也没找到 data/users.csv.migrated —— 无事可做")
        else:
            tag = "（预览，未写库）" if args.dry_run else ""
            print(f"来源：{rep['source']} {tag}")
            print(f"新建 {len(rep['created'])}: {', '.join(rep['created']) or '-'}")
            print(f"跳过（库里已有）{len(rep['skipped'])}: {', '.join(rep['skipped']) or '-'}")
            if rep["invalid"]:
                print(f"用户名非法 {len(rep['invalid'])}:")
                for line in rep["invalid"]:
                    print(f"  {line}")
    else:
        parser.print_help()
        sys.exit(1)
