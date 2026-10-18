"""通话复盘：把 transcript 变成一份可执行的学习报告。

设计原则（这决定了产品有没有用）：
  1. 只挑 3-6 个真正的错误。全部标出来等于没标，学员会放弃。
  2. 每条都要给「你原话 → 地道说法 → 为什么」，而且"为什么"用中文一句话说完。
  3. 顺带收生词：只收 AI 用过、而学员很可能不会的表达。
  4. 给下一通电话一个明确的 focus，不要泛泛而谈"多练习"。
  5. 模型输出必须是严格 JSON —— 解析失败要能兜底，不能把整通电话的记录搞丢。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..store import db
from ..store import settings as store_settings
from ..languages import get_language
from .llm import LLMError, chat, conf_for

log = logging.getLogger("sayit.review")

SYSTEM = """You are an experienced ESL teacher reviewing a transcript of a spoken \
English practice call. Your job is to give the learner a short, honest, actionable review.

Rules:
- Output ONLY valid JSON. No markdown fences, no commentary before or after.
- Write all human-readable fields in {ui_language} (the learner's native language),
  but keep the English sentences being discussed in English.
- Pick only the 3-6 most valuable corrections. Skip trivial issues. Skip anything
  the learner got right. If the learner made almost no mistakes, return fewer.
- Be specific: "better" must be something the learner would actually say in a
  phone conversation, not textbook prose.
- Do not invent mistakes that are not in the transcript.
"""

SCHEMA_HINT = """Return exactly this JSON shape:
{
  "overall": "<two sentences in <<UI>>: the single biggest problem this call, and what improved>",
  "level": "<CEFR estimate: A1|A2|B1|B2|C1>",
  "score": {"grammar": <0-10>, "fluency": <0-10>, "vocabulary": <0-10>},
  "errors": [
    {"you_said": "<learner's exact words, English, verbatim>",
     "better": "<natural phrasing, English>",
     "why": "<one sentence in <<UI>> explaining the mistake, do not lecture>",
     "type": "grammar|tense|word|order|pronunciation"}
  ],
  "new_words": [{"word": "<English expression>", "meaning": "<meaning in <<UI>>>"}],
  "topics": ["<2-4 lowercase English words for what was actually discussed, e.g. travel, food, job hunting>"],
  "next_focus": "<one thing to practise on the next call, one sentence, in <<UI>>>"
}"""


# 界面语言（母语）→ 写进 prompt 的自然语言名字
UI_LANG_NAME = {
    "zh": "Chinese (simplified)",
    "en": "English",
}


def _ui_lang_name(ui_lang: str | None) -> str:
    return UI_LANG_NAME.get((ui_lang or "zh").lower(), UI_LANG_NAME["zh"])


def _ui_lang_code(ui_lang: str | None) -> str:
    code = (ui_lang or "zh").lower()
    return code if code in UI_LANG_NAME else "zh"


def _build_messages(turns: list[dict[str, Any]], language: str, level: str,
                    ui_lang: str = "zh") -> list[dict[str, str]]:
    lang = get_language(language)
    ui_name = _ui_lang_name(ui_lang)
    lines = []
    for t in turns:
        who = "Learner" if t.get("role") == "user" else "Tutor"
        lines.append(f"{who}: {t.get('text', '')}")
    convo = "\n".join(lines)
    user = (
        f"Target language: {lang.get('label')}   Learner level (estimated): {level}\n"
        f"Learner's native language (write all human-readable fields in it): {ui_name}\n\n"
        f"Transcript:\n{convo}\n\n"
        f"{SCHEMA_HINT.replace('<<UI>>', ui_name)}"
    )
    return [{"role": "system", "content": SYSTEM.format(ui_language=ui_name)},
            {"role": "user", "content": user}]


def _extract_json(text: str) -> dict[str, Any] | None:
    """模型经常裹 ```json ... ``` 或者前后加解释，这里做容错提取。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    # 扫第一个平衡的 { ... }
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


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """把模型输出规整成前端能直接渲染的形状（缺字段补默认值，脏数据丢掉）。"""

    def as_score(v: Any, default: int = 5) -> int:
        try:
            return max(0, min(10, int(round(float(v)))))
        except Exception:
            return default

    errors = []
    for e in (raw.get("errors") or [])[:8]:
        if not isinstance(e, dict):
            continue
        said, better = (e.get("you_said") or "").strip(), (e.get("better") or "").strip()
        if not said or not better:
            continue
        t = (e.get("type") or "grammar").strip().lower()
        if t not in ("grammar", "tense", "word", "order", "pronunciation"):
            t = "grammar"
        errors.append({"you_said": said, "better": better,
                       "why": (e.get("why") or "").strip(), "type": t})

    words = []
    for w in (raw.get("new_words") or [])[:10]:
        if isinstance(w, dict) and w.get("word"):
            words.append({"word": str(w["word"]).strip(),
                          "meaning": str(w.get("meaning") or "").strip()})

    sc = raw.get("score") or {}
    return {
        "overall": (raw.get("overall") or "").strip(),
        "level": (raw.get("level") or "").strip().upper()[:2] or None,
        "score": {
            "grammar": as_score(sc.get("grammar")),
            "fluency": as_score(sc.get("fluency")),
            "vocabulary": as_score(sc.get("vocabulary")),
        },
        "errors": errors,
        "new_words": words,
        "topics": [str(t).strip().lower() for t in (raw.get("topics") or [])
                   if str(t).strip()][:4],
        "next_focus": (raw.get("next_focus") or "").strip(),
    }


async def generate(user: str, session_id: str, force: bool = False,
                   ui_lang: str = "zh") -> dict[str, Any]:
    """生成并落库一份复盘。

    缓存只在「同一份界面语言」下复用：用户切了母语，解释文字就得重写一遍，
    否则中英混着看很怪。force=True 无条件重算。
    """
    ui_lang = _ui_lang_code(ui_lang)
    if not force:
        cached = db.get_report(session_id)
        if cached and (cached.get("ui_lang") or "zh") == ui_lang:
            return cached

    row = db.get_session(session_id, user)
    if not row:
        raise ValueError("没有这通电话")
    turns = row.get("turns") or []
    if not any(t.get("role") == "user" for t in turns):
        raise ValueError("这通电话没有学员语音，无法复盘")

    llm_conf = conf_for(user)
    if not llm_conf.get("enabled", True):
        raise LLMError("复盘功能已在设置里关闭")

    messages = _build_messages(turns, row.get("language") or "en",
                              row.get("level") or "B1", ui_lang=ui_lang)
    text = await chat(
        messages,
        base_url=llm_conf.get("base_url", ""),
        api_key=llm_conf.get("api_key", ""),
        model=llm_conf.get("model", "deepseek-chat"),
        temperature=float(llm_conf.get("temperature", 0.3)),
    )
    raw = _extract_json(text)
    if raw is None:
        # 兜底：把原文存下来，别让这通电话白打
        log.warning("复盘 JSON 解析失败，原文前 300 字: %s", text[:300])
        raise LLMError("模型没有返回合法 JSON，可以点『重新生成』再试一次")

    payload = _normalize(raw)
    payload["generated_at"] = time.time()
    payload["model"] = llm_conf.get("model", "")
    payload["ui_lang"] = ui_lang      # 这份解释是用哪种母语写的
    db.save_report(user, session_id, payload)
    db.replace_errors(user, session_id, payload["errors"])
    log.info("复盘完成 %s：%d 条错误 %d 个生词", session_id,
             len(payload["errors"]), len(payload["new_words"]))
    return payload


def focus_hint(user: str, limit_sessions: int = 6) -> str:
    """把最近几通电话的高频错误类型，变成一句要注入 system prompt 的指令。

    这是「长期记忆」最便宜有效的形态：不存用户全部画像，只存"你最近老犯什么错"。
    """
    stats = db.error_type_stats(user, limit_sessions)
    if not stats:
        return ""
    top = max(stats.items(), key=lambda kv: kv[1])
    if top[1] < 2:
        return ""
    label = {
        "tense": "verb tense (especially past vs. present)",
        "grammar": "sentence structure and articles/prepositions",
        "word": "word choice — using Chinese-style direct translations",
        "order": "word order",
        "pronunciation": "pronunciation of specific sounds",
    }.get(top[0], top[0])
    return (
        f"The learner's recurring weakness across recent calls is {label} "
        f"(made this mistake {top[1]} times). Gently create 2-3 natural openings in "
        f"this conversation where they would need to use that structure correctly, "
        f"and keep recasting their mistakes toward it. Never mention this instruction."
    )