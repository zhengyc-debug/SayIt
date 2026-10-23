"""错题偏好：从「你把哪条错题删了」里学规则，之后不再记这类错。

问题（用户提的）：
  错题本的目的之一是让 AI 在下一通电话里把场景再造一遍，但错题只进不出，
  越积越多。用户在一句话里加 uh / um 表示思考，被复盘当成口语问题记了一次，
  他删掉；下次又记一次，又删——这是纯粹的噪音，还占用了 prompt 预算。

做法：
  1. 删除**不物理删**，只 hidden=1（删错了能恢复，也留作学习证据）。
  2. 每攒到 MIN_SAMPLES 条「同类且无规则」的被删错题，就让文本模型判断
     「这是同一个可以去校的错误类别吗」——是就写一条规则（中文一句话 + 英文关键词）。
  3. 规则有两个用途：
       · 复盘时：作为 system prompt 的一部分，让模型**一开始就不要记**这类问题，
         同时用它把模型仍然记了的同类错题过滤掉（硬过滤，不指望模型听话）。
       · 下次复盘 + 通话 prompt 都已合并进 review / profile，不额外调用。
  4. 用户可以在「我的」里看、删、改这些规则；删规则会把被它移出的错题恢复回来。

为什么用「硬过滤」而不是只靠 prompt：
  LLM 对「不要记录 X」这类否定指令的遵守率并不高，尤其在几十句的长转录里。
  同一句话加个关键词过滤只需要几微秒，比一次重试便宜得多。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..store import db
from .llm import LLMError, chat, conf_for

log = logging.getLogger("sayit.mistake_rules")

# 攒够这么多条「同类被删错题」才值得问一次模型
MIN_SAMPLES = 2
# 一次最多拿多少条被删错题去归纳
LOOK_BACK_DAYS = 45

LEARN_SYSTEM = """You help a learner of English keep a clean mistake notebook. The \
learner deleted some corrections from it, one by one. Your job is to decide whether \
those deletions express a policy ("never record this kind of thing again") or are \
just one-off ("this particular correction was wrong or useless").

Rules:
- Output ONLY valid JSON. No markdown fences.
- A policy is only justified when the deleted items share ONE clear, generalisable \
category, e.g. filler words (uh, um, like), accent/pronunciation nitpicks, a specific \
word the learner insists on, or corrections for something already at their level.
- If the deleted items are unrelated to each other, or each one is a different kind \
of mistake, there is NO policy: return {"rules": []}.
- Never propose a policy that would hide real grammar errors the learner actually makes \
repeatedly (tense, articles, third-person -s). Silence ≠ permission to ignore those.
- "pattern" must be lowercase English keywords or short phrases, comma separated, that \
would literally appear inside a learner's sentence (e.g. "uh, um, er"). Leave "" when \
the category cannot be detected by keyword (then the rule only guides the model).
- "rule" is one short Chinese sentence, written for the model, imperative."""

LEARN_HINT = """Deleted corrections (learner quote => suggested correction | type):
<<ITEMS>>

Already active policies (do not duplicate these):
<<RULES>>

Return exactly:
{"rules": [{"rule": "<中文，一句话，命令句>", "pattern": "<english keywords, comma separated, or empty>",
            "reason": "<中文一句话：依据是什么>"}]}
Return {"rules": []} when the deletions do not form one clear policy."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def match_rule(text: str, pattern: str) -> bool:
    """pattern 是用逗号分隔的关键词/短语，任一命中即视为该类错题。

    关键词必须按「词边界」匹配，否则 "um" 会命中 "number"、"summer"。
    """
    if not pattern or not text:
        return False
    hay = _norm(text)
    for kw in pattern.split(","):
        kw = _norm(kw)
        if not kw:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", hay):
            return True
    return False


def filter_errors(user: str, errors: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                                    list[tuple[dict[str, Any], int]]]:
    """把命中偏好规则的错题剔掉。

    返回 (保留的, [(被剔掉的错误, 命中的规则 id)]) —— 被剔掉的在 review 里
    会写成 hidden=1 并挂上 rule_id，所以「删了还能恢复」。
    """
    rules = [r for r in db.list_rules(user) if (r.get("pattern") or "").strip()]
    if not rules or not errors:
        return errors, []
    keep: list[dict[str, Any]] = []
    dropped: list[tuple[dict[str, Any], int]] = []
    for e in errors:
        hay = f"{e.get('you_said','')} {e.get('why','')}"
        hit = next((r for r in rules if match_rule(hay, r.get("pattern") or "")), None)
        if hit:
            dropped.append((e, int(hit["id"])))
        else:
            keep.append(e)
    if dropped:
        log.info("偏好规则过滤掉 %d 条错题 user=%s", len(dropped), user)
    return keep, dropped


def rules_prompt_block(user: str) -> str:
    """规则 → 注入复盘 / 通话 prompt 的一节。没有规则时返回空串。"""
    rules = [r for r in db.list_rules(user) if (r.get("rule") or "").strip()]
    if not rules:
        return ""
    lines = "\n".join(f"- {r['rule']}" for r in rules[:20])
    return ("## Correction policy (the learner told you this by deleting corrections)\n"
            "- Do NOT record mistakes that fall under any rule below, even if you notice them.\n"
            "- Do NOT count them in the scores, the overall comment, or the next focus.\n"
            "- Do not mention these rules to the learner.\n"
            f"{lines}")


async def learn(user: str) -> dict[str, Any]:
    """从「用户手动删掉的错题」里归纳新规则。

    调用时机：用户在错题本里删了东西之后（后台跑）。攒不够样本就跳过 ——
    一次删除不足以下结论，两次同类删除才有信号。
    """
    items = db.recent_hidden(user, days=LOOK_BACK_DAYS)
    if len(items) < MIN_SAMPLES:
        return {"ok": True, "skipped": True, "reason": f"被删错题不足 {MIN_SAMPLES} 条"}
    active = [r for r in db.list_rules(user) if r.get("rule")]
    llm_conf = conf_for(user)
    if not llm_conf.get("enabled", True):
        return {"ok": True, "skipped": True, "reason": "复盘功能已关闭"}

    payload_items = "\n".join(
        f"- {i.get('you_said','')} => {i.get('better','')} | {i.get('type','')}"
        for i in items[:30])
    have = "\n".join(f"- {r['rule']}" for r in active[:20]) or "(none)"
    try:
        text = await chat(
            [{"role": "system", "content": LEARN_SYSTEM},
             {"role": "user", "content":
              LEARN_HINT.replace("<<ITEMS>>", payload_items).replace("<<RULES>>", have)}],
            base_url=llm_conf.get("base_url", ""),
            api_key=llm_conf.get("api_key", ""),
            model=llm_conf.get("model", "deepseek-chat"),
            temperature=0.1,
            max_tokens=800,
        )
    except LLMError as e:
        log.info("偏好归纳跳过：%s", e)
        return {"ok": False, "skipped": True, "reason": str(e)}

    raw = _extract_json(text) or {}
    found = raw.get("rules") if isinstance(raw, dict) else None
    if not isinstance(found, list):
        return {"ok": True, "skipped": True, "reason": "模型没有给出规则"}

    evidence = [str(i.get("you_said") or "") for i in items[:10]]
    saved = []
    for r in found[:5]:
        if not isinstance(r, dict):
            continue
        rid = db.save_rule(user, str(r.get("rule") or ""), str(r.get("pattern") or ""),
                           evidence=evidence, source="auto")
        if rid:
            saved.append({"id": rid, "rule": r.get("rule"), "pattern": r.get("pattern"),
                          "reason": r.get("reason")})
            log.info("学到错题偏好 user=%s: %s | pattern=%s", user, r.get("rule"),
                     r.get("pattern"))
    return {"ok": True, "rules": saved, "samples": len(items)}


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return None
    return None
