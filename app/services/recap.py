"""通话摘要（recap）：让「继续上一通电话」有真正的上下文。

和 review.py 的区别（不要合并这两个文件）：
  · review：给**人**看的复盘（错误、评分、生词）。挂断后才写，可缓存、可重生成。
  · recap ：给**模型**看的状态（聊到哪、说过什么事实、下一步）。继续通话时读，
            只关心「下一段对话要接得上」，不关心教学评价。
  两者都读同一份 turns，但用户看复盘时不需要被塞一份上下文，模型也不需要看评分。

设计要点：
  1. 摘要要**短**：几百字的英文，直接塞进 system prompt，不能吃掉太多 token。
  2. 要保留**硬事实**（他叫什么、住哪、做过什么计划、你编过的细节）——这是续聊
     最容易穿帮的地方：模型忘了自己上一通说过的城市或人设，就会自相矛盾。
  3. 解析失败要能兜底：宁可退回「最后 12 句对话原文」也不要让续聊开天窗。
  4. 一条链上的多通电话（继续→继续）由调用方负责合并，本模块只处理「一段文本」。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..languages import get_language
from ..store import db
from .llm import LLMError, chat, conf_for

log = logging.getLogger("sayit.recap")

SYSTEM = """You are preparing a hand-off note for yourself. You are a voice-call \
conversation partner; a call with this learner was interrupted (time limit, bad \
connection, or they hung up mid-thought) and a new call will resume the SAME \
conversation in a few hours. Write the note you need to continue naturally.

Rules:
- Output ONLY valid JSON. No markdown fences, no commentary.
- Write the whole note in English (it is read by an English-speaking model).
- Keep it under 180 words total. It is a reminder, not a transcript.
- Only state things that actually happened in the transcript. Never invent.
- facts: hard details that would be embarrassing to contradict (names, cities,
  jobs, dates, plans, opinions they stated, details YOU made up about yourself).
- Never include teaching evaluation; another process handles that."""

SCHEMA_HINT = """Return exactly this JSON shape:
{
  "topic": "<4-8 word English label of what this conversation is about>",
  "summary": "<3-5 English sentences: what was discussed, in order, and how the learner reacted>",
  "facts": ["<short hard fact, e.g. 'Learner works as a QA engineer in Xi'an'>"],
  "open_threads": ["<unfinished thread: a question you asked that they never answered, a story cut off mid-way>"],
  "next_step": "<one English sentence: what you should do first when the call resumes>",
  "words_to_reuse": ["<2-5 English words/expressions that came up and should be recycled>"]
}"""


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


def _lines(turns: list[dict[str, Any]], limit: int | None = None) -> str:
    """turns → 'Learner: ... / Tutor: ...'。limit 只取最后 N 条（太长时保尾部）。"""
    rows = turns[-limit:] if limit else turns
    out = []
    for t in rows:
        who = "Learner" if t.get("role") == "user" else "Tutor"
        text = str(t.get("text") or "").strip()
        if text:
            out.append(f"{who}: {text}")
    return "\n".join(out)


def _clip(convo: str, limit: int = 12000) -> str:
    """超长时头尾都留：只留尾部会丢掉「这通电话是怎么聊起来的」，
    而续聊最需要的恰恰是开头设定的人物/话题和结尾的未完成话头。"""
    if len(convo) <= limit:
        return convo
    head, tail = convo[:limit // 3], convo[-(limit * 2) // 3:]
    return head + "\n\n[... middle of the call omitted ...]\n\n" + tail


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    def as_list(v: Any, n: int) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x or "").strip()][:n]

    return {
        "topic": str(raw.get("topic") or "").strip(),
        "summary": str(raw.get("summary") or "").strip(),
        "facts": as_list(raw.get("facts"), 10),
        "open_threads": as_list(raw.get("open_threads"), 5),
        "next_step": str(raw.get("next_step") or "").strip(),
        "words_to_reuse": as_list(raw.get("words_to_reuse"), 6),
    }


def fallback(turns: list[dict[str, Any]]) -> dict[str, Any]:
    """模型不可用时的兜底：直接把最后几句原文当成上下文。

    续聊效果肯定不如摘要，但比「完全不记得」强得多，而且 0 token、0 等待。
    """
    return {
        "topic": "",
        "summary": "The previous call was cut off. Last part of the transcript:\n"
                   + _lines(turns, 12),
        "facts": [],
        "open_threads": [],
        "next_step": "",
        "words_to_reuse": [],
        "fallback": True,
    }


async def summarize(user: str, turns: list[dict[str, Any]], language: str,
                    level: str = "B1") -> dict[str, Any]:
    """把一段（可能跨多通的）turns 压成一份 recap。

    抛 LLMError 时由调用方决定用 fallback()，不要在这里吞掉异常类型 ——
    上层要知道「这是模型的问题」而不是「这里没有数据」。
    """
    convo = _lines(turns)
    if not convo.strip():
        raise ValueError("没有可总结的对话")
    llm_conf = conf_for(user)
    if not llm_conf.get("enabled", True):
        raise LLMError("复盘功能已在设置里关闭")
    lang = get_language(language)
    user_msg = (
        f"Target language: {lang.get('label')}   Learner level: {level}\n\n"
        f"Transcript of the interrupted call:\n{_clip(convo)}\n\n"
        f"{SCHEMA_HINT}"
    )
    text = await chat(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": user_msg}],
        base_url=llm_conf.get("base_url", ""),
        api_key=llm_conf.get("api_key", ""),
        model=llm_conf.get("model", "deepseek-chat"),
        temperature=0.2,
        max_tokens=700,
    )
    raw = _extract_json(text)
    if raw is None:
        raise LLMError("摘要模型没有返回合法 JSON")
    out = _normalize(raw)
    if not out["summary"]:
        raise LLMError("摘要为空")
    return out


async def build_for_session(user: str, session_id: str) -> dict[str, Any]:
    """为「继续这一通」准备 recap，并把结果写进这通的 meta.summary。

    祖先链（继续的继续）的处理：
      · 每一通自己的 turns 全带上（没总结过的就在这里现总结）
      · 祖先已总结过的，把它的摘要并进来而不是重读它的全文 —— 链再长也不会爆 token
    """
    chain = _ancestors(user, session_id)          # 由远到近，不含自己
    parts: list[str] = []
    pending_turns: list[dict[str, Any]] = []
    for anc in chain:
        meta = anc.get("meta") or {}
        summ = meta.get("summary") or {}
        if summ.get("summary"):
            parts.append(_render_block(summ))
        else:
            pending_turns.extend(t for t in (anc.get("turns") or [])
                                 if (t.get("text") or "").strip())

    row = db.get_session(session_id, user)
    if not row:
        raise ValueError("没有这通电话")
    own_turns = [t for t in (row.get("turns") or []) if (t.get("text") or "").strip()]
    # 「继续」出来的这一通：只总结本次新增的内容，上一通的内容已经在上面的摘要里。
    # 不区分的话，每次续聊都会把整条链的原文重读一遍 —— 又慢又容易把旧的细节
    # 和新说的混在一起。
    is_continuation = bool((row.get("meta") or {}).get("last_call"))
    if is_continuation:
        pending_turns = own_turns
    else:
        pending_turns.extend(own_turns)

    own = await summarize(user, pending_turns, row.get("language") or "en",
                          row.get("level") or "B1")
    own["generated_at"] = time.time()
    own["turns_covered"] = len(pending_turns)
    own["new_only"] = is_continuation   # True = 只总结了本次新增（续接通话）
    if parts:
        # 链上有更早的摘要：并进 summary 里，让模型一次性看到全部来龙去脉
        own["earlier"] = parts
    db.set_session_meta(session_id, {"summary": own})
    log.info("生成续聊摘要 %s：%d 句 / %d 条事实", session_id,
             len(pending_turns), len(own.get("facts") or []))
    return own


def _ancestors(user: str, session_id: str, max_depth: int = 5) -> list[dict[str, Any]]:
    """沿 last_call 往上找，返回由远到近的祖先列表（不含自己）。"""
    out: list[dict[str, Any]] = []
    cur = db.get_session(session_id, user)
    seen = {session_id}
    while cur and len(out) < max_depth:
        prev = (cur.get("meta") or {}).get("last_call") or ""
        if not prev or prev in seen:
            break
        seen.add(prev)
        anc = db.get_session(prev, user)
        if not anc:
            break
        out.append(anc)
        cur = anc
    out.reverse()
    return out


def _render_block(s: dict[str, Any]) -> str:
    bits = []
    if s.get("topic"):
        bits.append(f"Topic: {s['topic']}")
    if s.get("summary"):
        bits.append(s["summary"])
    if s.get("facts"):
        bits.append("Facts: " + "; ".join(s["facts"]))
    if s.get("open_threads"):
        bits.append("Left hanging: " + "; ".join(s["open_threads"]))
    if s.get("next_step"):
        bits.append("Next step: " + s["next_step"])
    return "\n".join(bits)


def prompt_block(recap: dict[str, Any], last_called_at: float | None = None) -> str:
    """把 recap 变成注入 system prompt 的一节。空 recap 返回空串。"""
    if not recap or not (recap.get("summary") or recap.get("topic")):
        return ""
    head = ["## Where this conversation already got to",
            "You have ALREADY been talking to this learner. This is the same "
            "conversation continuing after the call dropped. Do NOT greet them as a "
            "stranger, do NOT re-introduce yourself, do NOT restart the topic from "
            "scratch, and never mention that you have a note."]
    if last_called_at:
        head.append(f"The previous part of the call happened on "
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last_called_at))}.")
    body = [head[0], *head[1:], _render_block(recap)]
    reuse = recap.get("words_to_reuse") or []
    if reuse:
        body.append("Naturally reuse these again when it fits: " + ", ".join(reuse))
    return "\n".join(x for x in body if x)
