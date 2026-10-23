"""SQLite 存储：通话记录 + 逐句文本 + 复盘 + 错题 + 画像。

所有表都有 user 列，每个账号只看得到自己的数据。
老库（没有 user 列）启动时自动迁移，历史数据归给 LEGACY_OWNER。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from .settings import LEGACY_OWNER
from ..paths import DATA_DIR, DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    user         TEXT NOT NULL DEFAULT '',
    started_at   REAL NOT NULL,
    ended_at     REAL,
    provider     TEXT,
    language     TEXT,
    scenario     TEXT,
    level        TEXT,
    voice        TEXT,
    turns        INTEGER DEFAULT 0,
    duration_ms  INTEGER DEFAULT 0,
    meta         TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

CREATE TABLE IF NOT EXISTS reports (
    session_id  TEXT PRIMARY KEY,
    user        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    model       TEXT,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    user        TEXT NOT NULL DEFAULT '',
    you_said    TEXT NOT NULL,
    better      TEXT NOT NULL,
    why         TEXT,
    type        TEXT NOT NULL,
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS profile (
    user          TEXT PRIMARY KEY,
    display_name  TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    updated_at    REAL
);

-- 「顺手记住」的生词本。每个词一张卡：音标 / 释义 / 例句 / 词形变化。
-- payload 存整份卡片 JSON（由 LLM 生成后缓存），ui_lang 记录解释用的是哪种母语。
CREATE TABLE IF NOT EXISTS words (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user        TEXT NOT NULL DEFAULT '',
    word        TEXT NOT NULL,
    session_id  TEXT,
    meaning     TEXT,
    ui_lang     TEXT DEFAULT 'zh',
    payload     TEXT,
    created_at  REAL NOT NULL
);
"""

# 老表 -> 需要补的列
_ADD_COLUMNS = {
    "sessions": [("user", "TEXT NOT NULL DEFAULT ''")],
    "reports": [("user", "TEXT NOT NULL DEFAULT ''")],
    "errors": [("user", "TEXT NOT NULL DEFAULT ''")],
}


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(c: sqlite3.Connection) -> None:
    """老库升级：补 user 列，并把已有行归给 LEGACY_OWNER。

    profile 老结构是 id=1 单行，新结构用 user 做主键，所以要整表重建。
    """
    for table, adds in _ADD_COLUMNS.items():
        if not c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        have = _cols(c, table)
        for col, decl in adds:
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                c.execute(f"UPDATE {table} SET user=? WHERE user=''", (LEGACY_OWNER,))

    if c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='profile'"
    ).fetchone():
        have = _cols(c, "profile")
        if "user" not in have:
            row = c.execute("SELECT display_name, notes, updated_at FROM profile"
                            " WHERE id=1").fetchone()
            c.execute("DROP TABLE profile")
            c.execute("""CREATE TABLE profile (
                user TEXT PRIMARY KEY, display_name TEXT DEFAULT '',
                notes TEXT DEFAULT '', updated_at REAL)""")
            if row:
                c.execute("INSERT INTO profile (user, display_name, notes, updated_at)"
                          " VALUES (?,?,?,?)",
                          (LEGACY_OWNER, row["display_name"], row["notes"],
                           row["updated_at"]))

    c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user"
              " ON sessions(user, started_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_errors_user ON errors(user, ts DESC)")
    # 生词要去重（同一个词在多通电话里出现只留最早上次出现的一条）
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_words_user_word"
              " ON words(user, lower(word))")


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


# ---------------------------------------------------------------------------
# 通话
# ---------------------------------------------------------------------------
def create_session(user: str, **kw: Any) -> str:
    sid = kw.get("id") or uuid.uuid4().hex[:16]
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO sessions (id, user, started_at, provider, language, scenario,"
            " level, voice, meta) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, user, time.time(), kw.get("provider", ""), kw.get("language", ""),
             kw.get("scenario", ""), kw.get("level", ""), kw.get("voice", ""),
             json.dumps(kw.get("meta") or {}, ensure_ascii=False)),
        )
    return sid


def add_turn(session_id: str, role: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    with _lock, _conn() as c:
        c.execute("INSERT INTO turns (session_id, role, text, ts) VALUES (?,?,?,?)",
                  (session_id, role, text, time.time()))
        c.execute("UPDATE sessions SET turns = turns + 1 WHERE id = ?", (session_id,))


def close_session(session_id: str, duration_ms: int = 0, meta: dict | None = None) -> None:
    with _lock, _conn() as c:
        row = c.execute("SELECT meta FROM sessions WHERE id=?", (session_id,)).fetchone()
        merged = {}
        if row and row["meta"]:
            try:
                merged = json.loads(row["meta"])
            except Exception:
                merged = {}
        merged.update(meta or {})
        c.execute("UPDATE sessions SET ended_at=?, duration_ms=?, meta=? WHERE id=?",
                  (time.time(), duration_ms, json.dumps(merged, ensure_ascii=False),
                   session_id))


def set_session_meta(session_id: str, patch: dict[str, Any]) -> None:
    """只合并 meta 里的几个字段，不动 ended_at / duration_ms / turns。

    续聊摘要就是走这里写的：它可能在该通电话结束后很久才生成。
    """
    if not patch:
        return
    with _lock, _conn() as c:
        row = c.execute("SELECT meta FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return
        merged: dict[str, Any] = {}
        if row["meta"]:
            try:
                merged = json.loads(row["meta"])
            except Exception:
                merged = {}
        merged.update(patch)
        c.execute("UPDATE sessions SET meta=? WHERE id=?",
                  (json.dumps(merged, ensure_ascii=False), session_id))


def list_sessions(user: str, limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id, started_at, ended_at, provider, language, scenario, level,"
            " turns, duration_ms, CASE WHEN json_valid(meta) THEN"
            "   json_extract(meta,'$.last_call') END AS last_call"
            " FROM sessions WHERE user=?"
            " ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (user, max(0, int(limit)), max(0, int(offset)))).fetchall()
    return [dict(r) for r in rows]


def delete_session(user: str, session_id: str) -> bool:
    """删掉一通电话：sessions + turns + reports + errors 一起删。

    从它「继续」出来的后续通话不删（那是另一通独立的记录），只把 meta.last_call
    断开并标上 detached，否则它们会指向一条已经不存在的父记录。
    """
    with _lock, _conn() as c:
        row = c.execute("SELECT id, meta FROM sessions WHERE id=? AND user=?",
                        (session_id, user)).fetchone()
        if not row:
            return False
        kids = c.execute(
            "SELECT id, meta FROM sessions WHERE user=? AND json_valid(meta)"
            " AND json_extract(meta,'$.last_call') = ?", (user, session_id)).fetchall()
        for k in kids:
            try:
                meta = json.loads(k["meta"] or "{}")
            except Exception:
                meta = {}
            meta.pop("last_call", None)
            meta["continuation"] = True          # 仍然是续接来的
            meta["ex_continuation"] = True       # 但上一通已被删除
            # 根被删了：自己当新根，链不断档
            meta.pop("root_id", None)
            c.execute("UPDATE sessions SET meta=? WHERE id=?",
                      (json.dumps(meta, ensure_ascii=False), k["id"]))
        c.execute("DELETE FROM turns WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM errors WHERE session_id=? AND user=?", (session_id, user))
        c.execute("DELETE FROM reports WHERE session_id=? AND user=?", (session_id, user))
        c.execute("DELETE FROM sessions WHERE id=? AND user=?", (session_id, user))
    return True


def delete_all_errors(user: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM errors WHERE user=?", (user,))
        return cur.rowcount or 0


def delete_all_words(user: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM words WHERE user=?", (user,))
        return cur.rowcount or 0


def count_sessions(user: str) -> int:
    with _lock, _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM sessions WHERE user=?",
                             (user,)).fetchone()["n"])


def get_session(session_id: str, user: str | None = None) -> dict[str, Any] | None:
    """传 user 时只能取到自己的那通（越权访问返回 None）。"""
    with _lock, _conn() as c:
        if user is None:
            s = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        else:
            s = c.execute("SELECT * FROM sessions WHERE id=? AND user=?",
                          (session_id, user)).fetchone()
        if not s:
            return None
        turns = c.execute("SELECT role, text, ts FROM turns WHERE session_id=?"
                          " ORDER BY id", (session_id,)).fetchall()
        # 谁是从这一通「继续」出来的（一条链可能好几个孩子）
        owner = s["user"] if "user" in s.keys() else (user or "")
        kids = c.execute(
            "SELECT id FROM sessions WHERE user=? AND id != ? AND json_valid(meta)"
            " AND json_extract(meta,'$.last_call') = ?"
            " ORDER BY started_at", (owner, session_id, session_id)).fetchall()
    out = dict(s)
    out["child_ids"] = [r["id"] for r in kids]
    try:
        out["meta"] = json.loads(out.get("meta") or "{}")
    except Exception:
        out["meta"] = {}
    out["turns"] = [dict(t) for t in turns]
    out["turn_count"] = len(turns)     # turn_count 才是条数，turns 是明细列表
    return out


# ---------------------------------------------------------------------------
# 复盘报告
# ---------------------------------------------------------------------------
def save_report(user: str, session_id: str, payload: dict[str, Any]) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO reports (session_id, user, created_at, model, payload)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET created_at=excluded.created_at,"
            " model=excluded.model, payload=excluded.payload",
            (session_id, user, time.time(), payload.get("model", ""),
             json.dumps(payload, ensure_ascii=False)),
        )


def get_report(session_id: str) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        row = c.execute("SELECT payload FROM reports WHERE session_id=?",
                        (session_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def replace_errors(user: str, session_id: str, errors: list[dict[str, Any]]) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM errors WHERE session_id=?", (session_id,))
        now = time.time()
        c.executemany(
            "INSERT INTO errors (session_id, user, you_said, better, why, type, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            [(session_id, user, e.get("you_said", ""), e.get("better", ""),
              e.get("why", ""), e.get("type", "grammar"), now) for e in errors],
        )


def error_type_stats(user: str, limit_sessions: int = 6) -> dict[str, int]:
    """最近 N 通电话里各类型错误出现次数。"""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT type, COUNT(*) n FROM errors WHERE user=? AND session_id IN ("
            "  SELECT id FROM sessions WHERE user=? ORDER BY started_at DESC LIMIT ?"
            ") GROUP BY type ORDER BY n DESC",
            (user, user, limit_sessions)).fetchall()
    return {r["type"]: r["n"] for r in rows}


def top_errors(user: str, limit: int = 50) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT e.you_said, e.better, e.why, e.type, e.ts, s.started_at"
            " FROM errors e JOIN sessions s ON s.id = e.session_id"
            " WHERE e.user=? ORDER BY e.ts DESC LIMIT ?", (user, limit)).fetchall()
    return [dict(r) for r in rows]


def stats_overview(user: str) -> dict[str, Any]:
    with _lock, _conn() as c:
        base = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(duration_ms),0) ms, COALESCE(SUM(turns),0) t"
            " FROM sessions WHERE user=? AND ended_at IS NOT NULL", (user,)).fetchone()
        days = c.execute(
            "SELECT date(started_at,'unixepoch','localtime') d, COUNT(*) n,"
            " COALESCE(SUM(duration_ms),0) ms FROM sessions"
            " WHERE user=? AND ended_at IS NOT NULL"
            " GROUP BY d ORDER BY d DESC LIMIT 14", (user,)).fetchall()
        reps = c.execute("SELECT COUNT(*) n FROM reports WHERE user=?",
                         (user,)).fetchone()
    return {
        "sessions": base["n"], "duration_ms": base["ms"], "turns": base["t"],
        "reports": reps["n"],
        "error_types": error_type_stats(user, 6),
        "by_day": [dict(r) for r in days],
    }


# ---------------------------------------------------------------------------
# 用户画像（只有手写字段落库；其余全部由上面几张表实时派生）
# ---------------------------------------------------------------------------
def get_profile_row(user: str) -> dict[str, Any]:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM profile WHERE user=?", (user,)).fetchone()
    return dict(row) if row else {"display_name": "", "notes": ""}


def update_profile_row(user: str, **fields: Any) -> dict[str, Any]:
    cur = get_profile_row(user)
    cur.update({k: v for k, v in fields.items()
                if k in ("display_name", "notes") and v is not None})
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO profile (user, display_name, notes, updated_at)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(user) DO UPDATE SET display_name=excluded.display_name,"
            " notes=excluded.notes, updated_at=excluded.updated_at",
            (user, cur.get("display_name", ""), cur.get("notes", ""), time.time()))
    return cur


def recent_reports(user: str, limit: int = 8) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT r.payload FROM reports r JOIN sessions s ON s.id = r.session_id"
            " WHERE r.user=? ORDER BY s.started_at DESC LIMIT ?",
            (user, limit)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# 生词本
# ---------------------------------------------------------------------------
def add_words(user: str, session_id: str, words: list[dict[str, Any]],
              ui_lang: str = "zh") -> int:
    """把复盘里的 new_words 落进生词本。

    同一个词（不分大小写）只留一条：重复出现时刷新 session_id/meaning，
    但不清空已经生成的卡片 payload —— 那是花 token 换来的，没必要重算。
    返回新增条数。
    """
    rows = []
    for w in words or []:
        word = str(w.get("word") or "").strip()
        if not word:
            continue
        rows.append((user, word, session_id, str(w.get("meaning") or "").strip(),
                     ui_lang, time.time()))
    if not rows:
        return 0
    added = 0
    with _lock, _conn() as c:
        for row in rows:
            cur = c.execute("SELECT id FROM words WHERE user=? AND lower(word)=lower(?)",
                            (user, row[1])).fetchone()
            if cur:
                c.execute("UPDATE words SET session_id=?, meaning=COALESCE(NULLIF(?,''),"
                          " meaning) WHERE id=?", (row[2], row[3], cur["id"]))
            else:
                c.execute("INSERT INTO words (user, word, session_id, meaning, ui_lang,"
                          " created_at) VALUES (?,?,?,?,?,?)", row)
                added += 1
    return added


def list_words(user: str, limit: int = 300) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id, word, session_id, meaning, ui_lang, payload, created_at"
            " FROM words WHERE user=? ORDER BY created_at DESC LIMIT ?",
            (user, max(1, int(limit)))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["card"] = json.loads(d.pop("payload") or "null")
        except Exception:
            d["card"] = None
        out.append(d)
    return out


def get_word(user: str, word: str) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        r = c.execute("SELECT id, word, session_id, meaning, ui_lang, payload, created_at"
                      " FROM words WHERE user=? AND lower(word)=lower(?)",
                      (user, (word or "").strip())).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["card"] = json.loads(d.pop("payload") or "null")
    except Exception:
        d["card"] = None
        d.pop("payload", None)
    return d


def save_word_card(user: str, word: str, card: dict[str, Any], ui_lang: str) -> None:
    with _lock, _conn() as c:
        row = c.execute("SELECT id FROM words WHERE user=? AND lower(word)=lower(?)",
                        (user, (word or "").strip())).fetchone()
        payload = json.dumps(card, ensure_ascii=False)
        if row:
            c.execute("UPDATE words SET payload=?, ui_lang=? WHERE id=?",
                      (payload, ui_lang, row["id"]))
        else:
            # 用户手动查了一个不在生词本里的词：顺手收进来
            c.execute("INSERT INTO words (user, word, session_id, meaning, ui_lang, payload,"
                      " created_at) VALUES (?,?,?,?,?,?,?)",
                      (user, (word or "").strip(), "", "", ui_lang, payload, time.time()))


def count_words(user: str) -> int:
    with _lock, _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM words WHERE user=?",
                             (user,)).fetchone()["n"])


def delete_word(user: str, word: str) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM words WHERE user=? AND lower(word)=lower(?)",
                        (user, (word or "").strip()))
        return cur.rowcount > 0
