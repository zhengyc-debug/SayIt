"""错题分类：后端全链路的真实验证（不启动服务、不 mock 页面）。

只验证后端 —— 页面 DOM 行为在浏览器里点一下就知道，不值得为它写 DOM 桩。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("SAYIT_ADMIN_PASSWORD", "adminpw123")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paths  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="sayit_cat_"))
paths.DATA_DIR = TMP
paths.DB_PATH = TMP / "sayit.db"

from app.store import db, accounts, settings  # noqa: E402
from app.services import review  # noqa: E402
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

print("[1] 类别体系")
check("类别数 >= 20", len(error_categories.CATEGORIES) >= 20)
check("介词搭配存在", "preposition" in error_categories.CATEGORIES)
check("中文名", error_categories.label("preposition") == "介词搭配")
check("映射回一级 type", error_categories.type_of("preposition") == "grammar")
check("不给类别时不落进 preposition",
      error_categories.normalize("nonsense") != "preposition")
check("中文名能归一", error_categories.normalize("冠词") == "article")

print("\n[2] 模型输出：全量保留（不再砍到 12）")
raw = []
for i in range(30):
    raw.append({"you_said": f"I go in Monday {i}", "better": f"I go on Monday {i}",
                "why": "介词", "type": "grammar", "category": "preposition", "priority": 20})
for i in range(8):
    raw.append({"you_said": f"I see him yesterday {i}", "better": f"I saw him {i}",
                "why": "时态", "type": "tense", "category": "tense_past", "priority": 5})
for i in range(4):
    raw.append({"you_said": f"I bought book {i}", "better": f"I bought a book {i}",
                "why": "冠词", "type": "grammar", "category": "article", "priority": 45})
raw.append({"you_said": "I go in Monday 0", "better": "dup", "why": "",
            "type": "grammar", "category": "preposition", "priority": 1})
raw.append({"you_said": "", "better": "empty", "type": "grammar"})

errs = review._normalize({"errors": raw})["errors"]
check("42 条全保留", len(errs) == 42, str(len(errs)))
check("重复句去重", sum(1 for e in errs if e["you_said"].endswith("Monday 0")) == 1)
check("空句丢弃", all(e["you_said"] for e in errs))
check("按 priority 升序", [e["priority"] for e in errs] == sorted(e["priority"] for e in errs))

print("\n[3] 正文只展示 15 条，但必须跨类别")
picked = review._pick_representative(errs, review.REPORT_ERROR_LIMIT)
check("恰好 15 条", len(picked) == 15, str(len(picked)))
check("覆盖全部 3 个类别", len({e["category"] for e in picked}) == 3,
      str({e["category"] for e in picked}))
check("小类别也露脸",
      all(c in {e["category"] for e in picked}
          for c in ("tense_past", "article")))

print("\n[4] 入库是 41 条（不是 15）")
sid = db.create_session(USER, language="en", level="B1")
db.replace_errors(USER, sid, errs)
rows = db.top_errors(USER, limit=500)
check("库里 42 条", len(rows) == 42, str(len(rows)))

print("\n[5] 按类别分组（错题本首页用的就是它）")
groups = db.error_groups(USER)
by = {g["category"]: g for g in groups}
check("3 组", len(groups) == 3, str(list(by)))
check("preposition 30 条", by.get("preposition", {}).get("count") == 30,
      str(by.get("preposition", {}).get("count")))
check("tense_past 8 条", by.get("tense_past", {}).get("count") == 8)
check("article 4 条", by.get("article", {}).get("count") == 4)
check("按条数降序", [g["count"] for g in groups] ==
      sorted([g["count"] for g in groups], reverse=True))
ts = [e["ts"] for e in by["preposition"]["errors"]]
check("组内最新在前", ts == sorted(ts, reverse=True))

print("\n[6] 每组明细截断不影响总数")
g2 = {g["category"]: g for g in db.error_groups(USER, per_category=5)}
check("明细 5 条", len(g2["preposition"]["errors"]) == 5)
check("count 仍 30", g2["preposition"]["count"] == 30)

print("\n[7] 老数据（无 category）进 other 组")
sid2 = db.create_session(USER, language="en")
db.replace_errors(USER, sid2, [{"you_said": "old", "better": "new", "why": "",
                                "type": "grammar"}])
g3 = {g["category"]: g for g in db.error_groups(USER)}
check("other 组存在", "other" in g3)
check("other 组 1 条", g3["other"]["count"] == 1)

print("\n===== " + ("全部通过" if not fails else f"{len(fails)} 项失败: {fails}") + " =====")
sys.exit(0 if not fails else 1)
