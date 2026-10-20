"""生词卡：音标 / 释义 / 例句 / 词形变化 / 惯用搭配。

为什么要单独一个服务：
  复盘里只给了「词 + 一句话中文意思」，够让人想起来，但不足以学会。
  用户点开一个词想看的是能背下来的东西 —— 音标（怎么念）、
  词形变化（went / gone / going 这一串）、例句（怎么用）。
  这些信息模型一句话就能给，没必要接第三方词典 API（还要多一把 Key）。

设计取舍：
  · 结果按词缓存进 words.payload。同一个词第二次点开是 0 token、0 延迟。
  · 只缓存一份「解释语言」：用户切了母语要重新生成，和复盘报告同一套规则。
  · LLM 不可用时返回 ok=false，前端只显示复盘里已有的释义 —— 不能因为
    没配文本模型 Key 就连「记住的词」都看不到。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..store import db
from .llm import LLMError, chat, conf_for

log = logging.getLogger("sayit.words")

SYSTEM = """You are an English learner's dictionary for a Chinese-speaking student. \
You produce compact, accurate dictionary cards. Output ONLY valid JSON."""

SCHEMA_HINT = """Return exactly this JSON shape (no markdown fences):
{
  "word": "<the word as given>",
  "phonetic_us": "/.../ phonetic transcription, American English, IPA",
  "phonetic_uk": "/.../ phonetic transcription, British English, IPA",
  "pos": "<part of speech as a learner would label it: n. / v. / adj. / adv. / phrase>",
  "meaning": "<meaning in <<UI>>, one short line>",
  "senses": [{"pos": "<n. / v. / ...>", "def": "<meaning in <<UI>>, one short line>"}],
  "forms": "<verb: base / past / past participle / present participle / third person singular; \
noun: plural; adjective: comparative / superlative. Leave out what does not apply. \
If the word has no inflections write 无>",
  "examples": [{"en": "<one natural example sentence in English, 6-14 words, \
everyday spoken register>", "zh": "<that sentence translated into <<UI>>>"}]
}
Rules:
- Use real IPA, not respelling. If unsure of one variety, give the same for both.
- 2 example sentences, one of them in a spoken-phone-call register.
- senses: at most 3, ordered by how common they are. If only one sense, still return one item.
- Never invent a word that does not exist. If the input is an expression \
(e.g. "break the ice"), treat it as one entry and put 无 in forms."""

UI_LANG_NAME = {"zh": "Chinese (simplified)", "en": "English"}


def _ui_name(ui_lang: str | None) -> str:
    code = (ui_lang or "zh").lower()
    return UI_LANG_NAME.get(code, UI_LANG_NAME["zh"])


def _ui_code(ui_lang: str | None) -> str:
    code = (ui_lang or "zh").lower()
    return code if code in UI_LANG_NAME else "zh"


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


def _normalize(raw: dict[str, Any], word: str) -> dict[str, Any]:
    def s(v: Any) -> str:
        return str(v or "").strip()

    senses = []
    for item in (raw.get("senses") or [])[:4]:
        if isinstance(item, dict) and s(item.get("def")):
            senses.append({"pos": s(item.get("pos")), "def": s(item.get("def"))})

    examples = []
    for item in (raw.get("examples") or [])[:3]:
        if isinstance(item, dict) and s(item.get("en")):
            examples.append({"en": s(item.get("en")), "zh": s(item.get("zh"))})
        elif isinstance(item, str) and item.strip():
            examples.append({"en": item.strip(), "zh": ""})

    return {
        "word": s(raw.get("word")) or word,
        "phonetic_us": s(raw.get("phonetic_us")),
        "phonetic_uk": s(raw.get("phonetic_uk")),
        "pos": s(raw.get("pos")),
        "meaning": s(raw.get("meaning")) or (senses[0]["def"] if senses else ""),
        "senses": senses,
        "forms": s(raw.get("forms")),
        "examples": examples,
    }


async def card(user: str, word: str, ui_lang: str = "zh", force: bool = False) -> dict[str, Any]:
    """取（必要时生成）一张词卡。返回 {ok, word, card, cached, message?}"""
    word = (word or "").strip()[:64]
    ui_lang = _ui_code(ui_lang)
    if not word:
        return {"ok": False, "message": "没有这个词", "word": word}

    row = db.get_word(user, word)
    cached = (row or {}).get("card")
    if cached and not force and (row or {}).get("ui_lang", "zh") == ui_lang:
        return {"ok": True, "word": word, "card": cached, "cached": True,
                "meaning": (row or {}).get("meaning", "")}

    conf = conf_for(user)
    if not conf.get("enabled", True):
        return {"ok": False, "word": word, "card": None,
                "message": "复盘功能已在设置里关闭，词卡需要文本模型"}

    ui_name = _ui_name(ui_lang)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f'Word: "{word}"\n'
            f"Write all explanatory text (meanings, translations) in {ui_name}.\n"
            f"{SCHEMA_HINT.replace('<<UI>>', ui_name)}"},
    ]
    try:
        text = await chat(
            messages,
            base_url=conf.get("base_url", ""),
            api_key=conf.get("api_key", ""),
            model=conf.get("model", "deepseek-chat"),
            temperature=0.2,
            max_tokens=900,
            timeout=45.0,
            response_format={"type": "json_object"},
        )
    except LLMError as e:
        log.info("词卡生成失败 %s: %s", word, e)
        return {"ok": False, "word": word, "card": None, "message": str(e)}

    raw = _extract_json(text)
    if raw is None:
        log.warning("词卡 JSON 解析失败 %s: %s", word, text[:200])
        return {"ok": False, "word": word, "card": None,
                "message": "模型没有返回合法 JSON，可以重试一次"}

    payload = _normalize(raw, word)
    db.save_word_card(user, word, payload, ui_lang)
    return {"ok": True, "word": word, "card": payload, "cached": False,
            "meaning": (row or {}).get("meaning", "")}
