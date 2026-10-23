"""用户画像 / 长期记忆 —— 全部基于 SQLite 里的既有数据派生，不额外调模型。

设计取舍（重要）：
  · 不做「用户档案表塞一堆字段然后慢慢过期」那一套。真正影响对话质量的只有三件事：
      1. 他现在大概什么水平（从最近几份复盘的 level 推）
      2. 他最近老犯什么错（从 errors 表聚合）
      3. 上次聊过什么（从最近几份复盘的 topics 取）
    这三样每次通话前现算，永远是最新的，不用维护同步逻辑。
  · 只留一个手写字段 notes：用户可以写「我下周要去面试」这种模型推不出来的信息。

成本：一次 SQLite 查询，0 token。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..store import db
from ..languages import get_language

TYPE_LABEL_EN = {
    "tense": "verb tense (past vs. present)",
    "grammar": "sentence structure / articles & prepositions",
    "word": "word choice (Chinese-style direct translation)",
    "order": "word order",
    "pronunciation": "pronunciation of specific sounds",
}


def snapshot(user: str, language: str = "en", recent: int = 8) -> dict[str, Any]:
    """当前画像。全部字段都是派生值，任何一项缺失都不影响其它项。"""
    prof_row = db.get_profile_row(user)
    reports = db.recent_reports(user, recent)

    levels = [r.get("level") for r in reports if r.get("level")]
    est_level = Counter(levels).most_common(1)[0][0] if levels else None

    topics: list[str] = []
    for r in reports[:4]:
        for t in (r.get("topics") or []):
            t = str(t).strip()
            if t and t not in topics:
                topics.append(t)

    err = db.error_type_stats(user, recent)
    overview = db.stats_overview(user)

    return {
        "display_name": prof_row.get("display_name") or "",
        "notes": prof_row.get("notes") or "",
        "est_level": est_level,
        "target_level": None,          # 由调用方用 active.level 填
        "recent_topics": topics[:6],
        "error_types": err,
        "top_weakness": (max(err.items(), key=lambda kv: kv[1])[0] if err else None),
        "last_focus": (reports[0].get("next_focus") if reports else "") or "",
        "sessions": overview.get("sessions", 0),
        "minutes": round((overview.get("duration_ms") or 0) / 60000),
        "reports": overview.get("reports", 0),
    }


def prompt_hint(user: str, language: str, selected_level: str) -> str:
    """把画像变成一段注入 system prompt 的指令。没有数据时返回空串。"""
    s = snapshot(user, language)
    lines: list[str] = []

    est, target = s.get("est_level"), (selected_level or "").upper()
    if est and target and est != target:
        order = ["A1", "A2", "B1", "B2", "C1", "C2"]
        try:
            d = order.index(est) - order.index(target)
        except ValueError:
            d = 0
        if d < 0:
            lines.append(
                f"Their measured level from recent calls ({est}) is lower than the "
                f"selected {target}. Speak a bit simpler and slower than {target} would imply.")
        elif d > 0:
            lines.append(
                f"Their measured level from recent calls ({est}) is higher than the "
                f"selected {target}. You can use richer vocabulary and push them a little.")

    weak = s.get("top_weakness")
    n = (s.get("error_types") or {}).get(weak, 0) if weak else 0
    if weak and n >= 2:
        lines.append(
            f"Recurring weakness across recent calls: {TYPE_LABEL_EN.get(weak, weak)} "
            f"({n} times). Create 2-3 natural chances for them to use it correctly, "
            f"and keep recasting their mistakes toward it.")

    topics = s.get("recent_topics") or []
    if topics:
        lines.append(
            "Topics from their recent calls: " + ", ".join(topics[:4]) +
            ". You may naturally ask how one of those went, but do not recite the list.")

    notes = (s.get("notes") or "").strip()
    if notes:
        lines.append(f"Things the learner told you about themselves: {notes[:300]}")

    # 错题偏好（AI 从「用户删了什么错题」里学到的规则）：通话时也别在这些点上
    # 刻意挖坑或反复暗示 —— 用户明确表示这些不算他要练的东西。
    try:
        from . import mistake_rules
        block = mistake_rules.rules_prompt_block(user)
        if block:
            lines.append("Correction policy for this learner:\n" + block)
    except Exception:
        pass

    if not lines:
        return ""
    return "## What you know about this learner (private, never mention it)\n" + \
           "\n".join(f"- {x}" for x in lines)