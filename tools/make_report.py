"""给某一通电话生成/重生成复盘报告（命令行）。

用法：
    python tools/make_report.py                 # 最近一通有语音的电话
    python tools/make_report.py <session_id>
    python tools/make_report.py --force         # 忽略缓存重新生成
    python tools/make_report.py --list          # 列出所有复盘过的通话

依赖 data/settings.json 里 llm 段的配置（或在 .env 里配 DEEPSEEK_API_KEY）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, review  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.llm import LLMError  # noqa: E402


def latest_with_speech() -> str | None:
    for s in db.list_sessions(30):
        row = db.get_session(s["id"])
        if row and any(t.get("role") == "user" for t in row.get("turns") or []):
            return s["id"]
    return None


async def run(session_id: str, force: bool) -> int:
    conf = load_settings().get("llm") or {}
    print(f"模型: {conf.get('model')} @ {conf.get('base_url')}  "
          f"key={'已配置' if conf.get('api_key') else '缺失'}")
    row = db.get_session(session_id)
    if not row:
        print(f"[X] 没有这通电话: {session_id}")
        return 2
    n_user = sum(1 for t in row.get("turns") or [] if t.get("role") == "user")
    print(f"会话 {session_id}  场景={row.get('scenario')} 级别={row.get('level')} "
          f"学员句数={n_user}")
    if not n_user:
        print("[X] 没有学员语音，无法复盘")
        return 2

    try:
        rep = await review.generate(session_id, force=force)
    except LLMError as e:
        print(f"[X] {e}")
        return 3

    print("\n" + "=" * 64)
    print(f"总评: {rep['overall']}")
    print(f"估计等级: {rep.get('level') or '-'}")
    s = rep["score"]
    print(f"分数: 语法 {s['grammar']}  流利 {s['fluency']}  词汇 {s['vocabulary']}")
    print(f"下次重点: {rep.get('next_focus')}")
    print("-" * 64)
    for i, e in enumerate(rep["errors"], 1):
        print(f"{i}. [{e['type']}] 你说: {e['you_said']}")
        print(f"   地道: {e['better']}")
        print(f"   原因: {e['why']}")
    print("-" * 64)
    for w in rep["new_words"]:
        print(f"  · {w['word']} — {w['meaning']}")
    print("=" * 64)
    print("\n原始 JSON:")
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:1200])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id", nargs="?")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    db.init_db()
    if args.list:
        for s in db.list_sessions(30):
            rep = db.get_report(s["id"])
            mark = f"复盘✓ {len(rep['errors'])}错" if rep else "—"
            print(f"{s['id']}  {s['scenario']:<10} {s['turns']:>3}轮  {mark}")
        return 0

    sid = args.session_id or latest_with_speech()
    if not sid:
        print("[X] 找不到有学员语音的通话")
        return 2
    return asyncio.run(run(sid, args.force))


if __name__ == "__main__":
    raise SystemExit(main())
