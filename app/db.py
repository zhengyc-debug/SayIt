"""SQLite 存储：通话记录 + 逐句文本。

暂时只用标准库，不引 ORM。表结构简单到可以直接看。
Stage 2/3 的复盘、错题本、用户画像都会挂在这两张表上。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import DB_PATH, DATA_DIR

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
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
    created_at  REAL NOT NULL,
    model       TEXT,
    payload     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    you_said    TEXT NOT NULL,
    better      TEXT NOT NULL,
    why         TEXT,
    type        TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_session ON errors(session_id);
CREATE INDEX IF NOT EXISTS idx_errors_type ON errors(type);

CREATE TABLE IF NOT EXISTS profile (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    display_name  TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    updated_at    REAL
);
"""


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


def create_session(**kw: Any) -> str:
    sid = kw.get("id") or uuid.uuid4().hex[:16]
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO sessions (id, started_at, provider, language, scenario, level, voice, meta)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (sid, time.time(), kw.get("provider", ""), kw.get("language", ""),
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
                  (time.time(), duration_ms, json.dumps(merged, ensure_ascii=False), session_id))


def list_sessions(limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id, started_at, ended_at, provider, language, scenario, level, turns, duration_ms"
            " FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (max(0, int(limit)), max(0, int(offset)))).fetchall()
    return [dict(r) for r in rows]


def count_sessions() -> int:
    with _lock, _conn() as c:
        return int(c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"])


def get_session(session_id: str) -> dict[str, Any] | None:
    with _lock, _conn() as c:
        s = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not s:
            return None
        turns = c.execute("SELECT role, text, ts FROM turns WHERE session_id=? ORDER BY id",
                          (session_id,)).fetchall()
    out = dict(s)
    try:
        out["meta"] = json.loads(out.get("meta") or "{}")
    except Exception:
        out["meta"] = {}
    out["turns"] = [dict(t) for t in turns]
    out["turn_count"] = len(turns)          # 注意：turn_count 才是条数，turns 是明细列表
    return out


# ===========================================================================
# 复盘报告
# ===========================================================================
def save_report(session_id: str, payload: dict[str, Any]) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO reports (session_id, created_at, model, payload) VALUES (?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET created_at=excluded.created_at,"
            " model=excluded.model, payload=excluded.payload",
            (session_id, time.time(), payload.get("model", ""),
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


def replace_errors(session_id: str, errors: list[dict[str, Any]]) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM errors WHERE session_id=?", (session_id,))
        now = time.time()
        c.executemany(
            "INSERT INTO errors (session_id, you_said, better, why, type, ts)"
            " VALUES (?,?,?,?,?,?)",
            [(session_id, e.get("you_said", ""), e.get("better", ""),
              e.get("why", ""), e.get("type", "grammar"), now) for e in errors],
        )


def error_type_stats(limit_sessions: int = 6) -> dict[str, int]:
    """最近 N 通电话里各类型错误出现次数。"""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT type, COUNT(*) n FROM errors WHERE session_id IN ("
            "  SELECT id FROM sessions ORDER BY started_at DESC LIMIT ?"
            ") GROUP BY type ORDER BY n DESC", (limit_sessions,)).fetchall()
    return {r["type"]: r["n"] for r in rows}


def top_errors(limit: int = 50) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT e.you_said, e.better, e.why, e.type, e.ts, s.started_at"
            " FROM errors e JOIN sessions s ON s.id = e.session_id"
            " ORDER BY e.ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats_overview() -> dict[str, Any]:
    with _lock, _conn() as c:
        base = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(duration_ms),0) ms, COALESCE(SUM(turns),0) t"
            " FROM sessions WHERE ended_at IS NOT NULL").fetchone()
        days = c.execute(
            "SELECT date(started_at,'unixepoch','localtime') d, COUNT(*) n,"
            " COALESCE(SUM(duration_ms),0) ms FROM sessions"
            " WHERE ended_at IS NOT NULL"
            " GROUP BY d ORDER BY d DESC LIMIT 14").fetchall()
        reps = c.execute("SELECT COUNT(*) n FROM reports").fetchone()
    return {
        "sessions": base["n"], "duration_ms": base["ms"], "turns": base["t"],
        "reports": reps["n"],
        "error_types": error_type_stats(6),
        "by_day": [dict(r) for r in days],
    }


# ===========================================================================
# 用户画像（只有手写字段落库；其余全部由上面几张表实时派生）
# ===========================================================================
def get_profile_row() -> dict[str, Any]:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM profile WHERE id=1").fetchone()
    return dict(row) if row else {"display_name": "", "notes": ""}


def update_profile_row(**fields: Any) -> dict[str, Any]:
    cur = get_profile_row()
    cur.update({k: v for k, v in fields.items()
                if k in ("display_name", "notes") and v is not None})
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO profile (id, display_name, notes, updated_at) VALUES (1,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,"
            " notes=excluded.notes, updated_at=excluded.updated_at",
            (cur.get("display_name", ""), cur.get("notes", ""), time.time()))
    return cur


def recent_reports(limit: int = 8) -> list[dict[str, Any]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT r.payload FROM reports r JOIN sessions s ON s.id = r.session_id"
            " ORDER BY s.started_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except Exception:
            pass
    return out
