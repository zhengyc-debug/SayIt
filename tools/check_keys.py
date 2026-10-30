#!/usr/bin/env python3
"""查库里每个用户配置了哪些 API Key（只显示首尾 4 位，不打全量）。

用法（在项目根目录 /opt/SayIt 下）：
    python3 tools/check_keys.py            # 所有用户
    python3 tools/check_keys.py admin      # 只看某个用户
"""
import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "sayit.db"


def mask(k: object) -> str:
    s = str(k or "").strip()
    if not s:
        return "没有 Key"
    if len(s) <= 8:
        return "有 Key (太短不显示)"
    return f"有 Key  {s[:4]}********{s[-4:]}"


def main() -> int:
    if not DB.is_file():
        print(f"找不到数据库：{DB}")
        return 1
    only = sys.argv[1] if len(sys.argv) > 1 else None

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT user, settings, updated_at FROM user_settings"
        + (" WHERE user=?" if only else "") + " ORDER BY user",
        (only,) if only else (),
    ).fetchall()

    if not rows:
        print(f"user_settings 表里没有 {only} 的记录")
        return 0

    for r in rows:
        s = json.loads(r["settings"] or "{}")
        print(f"\n== {r['user']} ==  (updated {r['updated_at']})")
        provs = s.get("providers") or {}
        for pid, conf in provs.items():
            if isinstance(conf, dict):
                print(f"  {pid:<16} {mask(conf.get('api_key'))}")
        llm = s.get("llm") or {}
        print(f"  {'llm(复盘用)':<16} {mask(llm.get('api_key'))}")
        # 当前生效的语音供应商
        active = (s.get("active") or {}).get("provider")
        if active:
            got = (provs.get(active) or {}).get("api_key")
            print(f"  → 当前语音供应商：{active}（{'已配 Key，可以通话' if got else '没有 Key，点通话会报错'}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
