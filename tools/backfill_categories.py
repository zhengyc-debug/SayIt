#!/usr/bin/env python3
"""把历史错题补上二级分类（category）。

历史错题指的是「加分类功能之前」入库的行：errors 表里 category 为空或 NULL。
错题本页面会把它们全归进「其他」组，点开看不到分类价值 —— 这个脚本负责补上。

两种归法，按顺序尝试：
  1. 模型归类（准确）：把每条的老句子 + 改法 + 原因交给文本模型，让它从
     app/error_categories.py 的清单里挑一个 code。需要用户配过 llm.api_key。
  2. 规则兜底（不联网）：模型不可用 / 单条失败时，按关键词猜（介词、冠词、
     时态词 …）。猜不出来就留 "other"，下次再跑还能重试。

用法（在项目根目录下）：
    python3 tools/backfill_categories.py --dry-run      # 只统计，不写库
    python3 tools/backfill_categories.py --user admin   # 只处理一个用户
    python3 tools/backfill_categories.py                # 真写真跑
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import error_categories  # noqa: E402
from app.services.llm import LLMError, chat  # noqa: E402
from app.store import settings as store_settings  # noqa: E402

def _db_path() -> Path:
    """在调用时取路径，不在 import 期写死。

    写死 data/sayit.db 会让测试（改了 paths.DATA_DIR）和 `--db` 参数全部失效，
    读到的是真实库 —— 那是最危险的一种 bug：预览时看着没问题，真跑却改了别的库。
    """
    from app import paths
    return Path(paths.DB_PATH)

# 规则兜底：按句子里的关键词猜类别。只求「比 other 强」，不追求完美。
# 顺序有意义 —— 先匹配更具体的信号。
_RULES: list[tuple[tuple[str, ...], str | None]] = [
    ((" in monday", " on monday", " in the morning", " at night", " arrive in",
      " arrive to", " talk with", " talk to", " depend on", " good at",
      " interested in", " listen to"), "preposition"),
    ((" a ", " an ", " the "), None),          # 冠词太泛，交给模型，不用规则猜
    ((" yesterday", " last night", " last week", " ago"), "tense_past"),
    ((" have ", " has ", " had "), "tense_perfect"),
    ((" am ", " is ", " are ", " was ", " were "), "agreement"),
    ((" can ", " could ", " should ", " must ", " may "), "modal"),
    ((" more ", " most ", " -er "), "comparison"),
    ((" he ", " she ", " it ", " they "), "pronoun"),
]


def rule_guess(said: str, why: str) -> str | None:
    """不联网的粗归。返回 None 表示猜不出来。"""
    blob = f" {said} {why} ".lower()
    for keys, cat in _RULES:
        if cat is None:
            continue
        if any(k in blob for k in keys):
            return cat
    return None


def load_missing(user: str | None = None) -> list[dict]:
    c = sqlite3.connect(_db_path())
    c.row_factory = sqlite3.Row
    sql = ("SELECT id, user, you_said, better, why, type FROM errors"
           " WHERE COALESCE(category,'')=''")
    args: list = []
    if user:
        sql += " AND user=?"
        args.append(user)
    sql += " ORDER BY user, ts"
    return [dict(r) for r in c.execute(sql, args)]


async def classify_with_llm(rows: list[dict], user: str) -> dict[int, str]:
    """一次把同一个用户的全部老错题交给模型归类，返回 {id: category}。

    一批发给模型而不是逐条：一条一条调 API 又慢又贵，而这些句子都很短，
    50 条一次也就一千多 token。失败就返回空字典，让调用方走规则兜底。
    """
    conf = store_settings.load_settings(user).get("llm") or {}
    if not conf.get("api_key"):
        return {}

    items = "\n".join(
        f'{i}. said: {r["you_said"]}  ||  better: {r["better"]}'
        f'  ||  why: {r["why"] or "-"}  ||  type: {r["type"]}'
        for i, r in enumerate(rows))

    system = (
        "You are labelling English-learner mistakes with a category code. "
        "Output ONLY a JSON object mapping each item number (as a string) to exactly "
        "one category code from the list. No commentary.\n\n"
        "Category codes:\n" + error_categories.prompt_block()
    )
    payload = f"Items:\n{items}\n\nReturn JSON like {{\"0\": \"preposition\", \"1\": \"article\"}}"

    try:
        text = await chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": payload}],
            base_url=conf.get("base_url", ""),
            api_key=conf.get("api_key", ""),
            model=conf.get("model", "deepseek-chat"),
            temperature=0.0,
            max_tokens=2000,
        )
    except LLMError as e:
        print(f"    模型不可用，改用规则兜底：{e}")
        return {}

    # 容错解析：模型可能包 ```json
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lower().startswith("json") else t
    try:
        mapping = json.loads(t)
    except Exception:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end < 0:
            print("    模型返回不是 JSON，改用规则兜底")
            return {}
        try:
            mapping = json.loads(t[start:end + 1])
        except Exception:
            print("    模型返回不是合法 JSON，改用规则兜底")
            return {}

    out: dict[int, str] = {}
    for k, v in mapping.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(rows):
            out[rows[idx]["id"]] = error_categories.normalize(str(v))
    return out


def apply(updates: dict[int, str]) -> int:
    if not updates:
        return 0
    c = sqlite3.connect(_db_path())
    with c:
        c.executemany("UPDATE errors SET category=? WHERE id=?",
                      [(cat, eid) for eid, cat in updates.items()])
    return len(updates)


async def main() -> int:
    ap = argparse.ArgumentParser(description="给历史错题补二级分类")
    ap.add_argument("--user", default="", help="只处理某个用户（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--no-llm", action="store_true", help="只用关键词规则，不调模型")
    args = ap.parse_args()

    db_file = _db_path()
    if not db_file.is_file():
        print(f"找不到数据库：{db_file}")
        return 1

    rows = load_missing(args.user or None)
    if not rows:
        print("没有需要补分类的历史错题（category 都已有值）。")
        return 0

    by_user: dict[str, list[dict]] = {}
    for r in rows:
        by_user.setdefault(r["user"], []).append(r)

    total = 0
    for user, items in by_user.items():
        print(f"\n== {user}：{len(items)} 条待分类 ==")
        updates: dict[int, str] = {}
        if not args.no_llm:
            updates = await classify_with_llm(items, user)
            if updates:
                print(f"    模型归类了 {len(updates)} 条")
        # 剩下没归的用规则兜底
        guess = 0
        for r in items:
            if r["id"] in updates:
                continue
            g = rule_guess(r["you_said"], r["why"] or "")
            if g:
                updates[r["id"]] = g
                guess += 1
        if guess:
            print(f"    规则归了 {guess} 条")
        rest = [r for r in items if r["id"] not in updates]
        if rest:
            print(f"    {len(rest)} 条猜不出来，保留在「其他」组（可再跑一次）")

        # 打印结果预览
        for r in items:
            cat = updates.get(r["id"], "other")
            print(f"      [{cat:<18}] {r['you_said'][:52]}")

        if args.dry_run:
            print("    (dry-run，未写库)")
        else:
            total += apply(updates)

    if args.dry_run:
        print(f"\n共 {len(rows)} 条待处理（dry-run，数据库未改动）")
    else:
        print(f"\n已更新 {total} 条的分类")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
