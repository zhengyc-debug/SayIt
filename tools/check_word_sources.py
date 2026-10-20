"""验证词卡三级数据源：有道 → dictionaryapi → 大模型（可关），以及缓存行为。

用项目 venv 跑（要 import app 包）：
    env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe -X utf8 tools/check_word_sources.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import words  # noqa: E402
from app.store import db  # noqa: E402

USER = "tester"


async def main() -> int:
    print("=" * 70)
    print("1) 有道命中：常用词/短语都不该走模型")
    for w in ["receive", "break the ice", "captivating", "went", "so far"]:
        r = await words.card(USER, w, ui_lang="zh", force=True)
        c = r.get("card") or {}
        print(f"  {w:16s} src={r.get('source')!r:10s} ok={r.get('ok')} "
              f"us={c.get('phonetic_us')!r} forms={bool(c.get('forms'))} "
              f"ex={len(c.get('examples') or [])} audio={bool(c.get('audio'))}")

    print("\n2) 有道查不到的词 → 应落到 dictionaryapi（不该是 llm）")
    # 造一个有道没有、Wiktionary 有的词；再试一个纯生造词
    for w in ["serendipitous", "qwxzvbnm"]:
        r = await words.card(USER, w, ui_lang="en", force=True)   # en 界面直接跳有道
        print(f"  {w:16s} src={r.get('source')!r:10s} ok={r.get('ok')} msg={r.get('message','')[:40]}")

    print("\n3) 关掉大模型兜底：生造词应该直接失败，且不产生 token")
    r = await words.card(USER, "qwxzvbnm", ui_lang="zh", force=True, allow_llm=False)
    print(f"  qwxzvbnm allow_llm=False -> ok={r.get('ok')} msg={r.get('message')}")

    print("\n4) 缓存：第二次同词同语言应为 cached=True 且来源不变")
    r1 = await words.card(USER, "receive", ui_lang="zh")
    r2 = await words.card(USER, "receive", ui_lang="zh")
    print(f"  first={r1.get('cached')} second={r2.get('cached')} src={r2.get('source')}")

    print("\n5) 库里落盘的卡片（抽查一条完整结构）")
    row = db.get_word(USER, "receive")
    import json
    print(json.dumps(row["card"], ensure_ascii=False, indent=1)[:900])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
