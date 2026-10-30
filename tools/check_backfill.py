"""backfill_categories.py 的真实验收：不联网，用一个本地假 LLM 端点走完整链路。

考点：
  1. 模型返回的 JSON（含 ```json 包裹）能被解析
  2. 模型给的非法 code 会被 normalize
  3. 模型没覆盖到的条目会走规则兜底
  4. 真写库后 category 有值，再跑一次就没有待处理项（幂等）
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SAYIT_ADMIN_PASSWORD", "adminpw123")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paths  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sayit_backfill_"))
paths.DATA_DIR = TMP
paths.DB_PATH = TMP / "sayit.db"

from app.store import db, accounts, settings  # noqa: E402
from app import error_categories  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{extra}]" if extra and not ok else ""))
    if not ok:
        fails.append(name)


db.init_db()
settings.init_settings(TMP)
accounts.init_accounts()
USER = "admin"

# 造 5 条「老」错题：category 为空
old = [
    {"you_said": "I go to school in Monday", "better": "I go to school on Monday",
     "why": "具体某天用 on", "type": "grammar"},
    {"you_said": "I bought book", "better": "I bought a book",
     "why": "缺冠词", "type": "grammar"},
    {"you_said": "I see him yesterday", "better": "I saw him yesterday",
     "why": "过去式", "type": "tense"},
    {"you_said": "he go to work by bus", "better": "he goes to work by bus",
     "why": "主谓一致", "type": "grammar"},
    {"you_said": "xyzzy unknown sentence", "better": "whatever", "why": "", "type": "word"},
]
sid = db.create_session(USER, language="en", level="B1")
db.replace_errors(USER, sid, old)

rows = db.top_errors(USER, limit=100)
check("[1] 老错题 category 为空", all((r.get("category") or "") == "" for r in rows),
      str([r.get("category") for r in rows]))
check("    共 5 条", len(rows) == 5, str(len(rows)))

# ---- 用假 LLM 端点验证 classify_with_llm ----
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
import backfill_categories as bf  # noqa: E402

# 直接替换 chat，模拟模型返回：包 ```json、故意给两个合法 + 一个非法 code，
# 其余不给（测规则兜底）
fake_reply = "```json\n" + json.dumps({
    "0": "preposition",
    "1": "article",
    "2": "NOT_A_REAL_CODE",
}) + "\n```"


async def fake_chat(messages, **kw):
    return fake_reply


bf.chat = fake_chat

# 给用户配一个假 Key，让 classify_with_llm 认为模型可用
conf = settings.load_settings(USER)
conf.setdefault("llm", {})["api_key"] = "sk-fake-for-test"
conf["llm"]["base_url"] = "http://127.0.0.1:1/v1"
settings.update_settings(USER, {"llm": conf["llm"]})

missing = bf.load_missing(USER)
check("[2] 找到 5 条待分类", len(missing) == 5, str(len(missing)))

mapping = asyncio.run(bf.classify_with_llm(missing, USER))
check("[3] 模型解析出 3 条", len(mapping) == 3, str(len(mapping)))
ids = [r["id"] for r in missing]
check("    第 1 条 → preposition", mapping.get(ids[0]) == "preposition")
check("    第 2 条 → article", mapping.get(ids[1]) == "article")
check("    非法 code 被规范化（不是原样）", mapping.get(ids[2]) != "NOT_A_REAL_CODE",
      str(mapping.get(ids[2])))
check("    非法 code 不落进 preposition", mapping.get(ids[2]) != "preposition",
      str(mapping.get(ids[2])))

# ---- 规则兜底 ----
print("\n[4] 规则兜底")
check("介词关键词能猜", bf.rule_guess("I go in Monday", "") == "preposition")
check("过去式关键词能猜", bf.rule_guess("I see him yesterday", "") == "tense_past")
check("猜不出的返回 None", bf.rule_guess("xyzzy unknown", "") is None)

# ---- 真写库 ----
print("\n[5] 写库 + 幂等")
all_updates = dict(mapping)
for r in missing:
    if r["id"] not in all_updates:
        g = bf.rule_guess(r["you_said"], r["why"] or "")
        if g:
            all_updates[r["id"]] = g
n = bf.apply(all_updates)
check("写入了 4 条（3 模型 + 1 规则）", n == 4, str(n))

after = db.top_errors(USER, limit=100)
with_cat = [r for r in after if r.get("category")]
check("现在有 4 条带分类", len(with_cat) == 4, str(len(with_cat)))
check("剩下 1 条仍是空（待重跑）",
      sum(1 for r in after if not r.get("category")) == 1)
check("介词那条第 1 条归对了",
      next(r["category"] for r in after if "Monday" in r["you_said"]) == "preposition")

# 再跑一次 load_missing：只剩猜不出的那条
missing2 = bf.load_missing(USER)
check("二次运行只剩 1 条待处理", len(missing2) == 1, str(len(missing2)))
check("幂等：已分类的不会被重复处理",
      all(r["you_said"] != "I go to school in Monday" for r in missing2))

print("\n[6] 分组页能立刻反映新分类")
grouped = db.error_groups(USER)
cats = {g["category"] for g in grouped}
check("分组里出现 preposition", "preposition" in cats, str(cats))
check("分组里出现 article", "article" in cats)
check("未分类的进 other", "other" in cats)
check("标签可读", error_categories.label("preposition") == "介词搭配")

print("\n===== " + ("全部通过" if not fails else f"{len(fails)} 项失败: {fails}") + " =====")
sys.exit(0 if not fails else 1)
