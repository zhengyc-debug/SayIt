"""通话复盘：把 transcript 变成一份可执行的学习报告。

设计原则（这决定了产品有没有用）：
  1. 全量覆盖，但不是全量罗列：一份长录音按块切开，每块都必须逐句看完，
     再由最后一遍汇总。**块与块之间不能只取「前 N 条」** —— 那样一通
     20 分钟的电话只会复盘开头几分钟，学员后半段的错误全丢。
  2. 只挑 3-6 个最值得改正的错误输出给人看。全部标出来等于没标，学员会放弃。
  3. 每条都要给「你原话 → 地道说法 → 为什么」，而且"为什么"用中文一句话说完。
  4. 顺带收生词：只收 AI 用过、而学员很可能不会的表达。
  5. 给下一通电话一个明确的 focus，不要泛泛而谈"多练习"。
  6. 模型输出必须是严格 JSON —— 解析失败要能兜底，不能把整通电话的记录搞丢。
  7. 「继续」出来的通话只复盘本次新增内容：上一通的 turns 已经被上一份复盘
     覆盖过，重复算进去会让同一个错误被反复记进错题本。
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
from . import mistake_rules

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


# 单块放多少条字幕。DeepSeek 128k 上下文，一次全塞进去也能行，
# 但 60 分钟的对话全塞会让模型「跳读」；切成块逐块看，覆盖更稳。
CHUNK_TURNS = 60
# 一份复盘模型最多扫多少块（约 60 句 × 24 = 1400 句，远超 60 分钟通话的量）
MAX_CHUNKS = 24

SCAN_SYSTEM = """You are skimming a chunk of a spoken language-practice call for a \
teacher who will later write the learner's review. Read EVERY line, start to end.

Rules:
- Output ONLY valid JSON. No markdown fences, no commentary.
- Only report things the LEARNER actually said. Do not comment on the tutor.
- Quote the learner verbatim in "said".
- Be terse. This is a note-taking pass, not a review.
"""

SCAN_HINT = """Return exactly this JSON shape:
{
  "errors": [{"said": "<learner's exact words>", "better": "<natural rewrite>",
              "why": "<one short sentence in <<UI>> what was wrong>",
              "type": "grammar|tense|word|order|pronunciation"}],
  "phrases": ["<notable expressions the tutor used that a learner at this level probably did not know>"],
  "topics": ["<lowercase English keywords for what was discussed>"]
}
Return fewer items when the chunk is clean. Never invent."""


def split_chunks(turns: list[dict[str, Any]], size: int = CHUNK_TURNS) -> list[list[dict[str, Any]]]:
    """按字幕条数切块。只在条数上切，不按字符截断 —— 截断会把句子劈两半。"""
    rows = [t for t in turns if str(t.get("text") or "").strip()]
    if not rows:
        return []
    return [rows[i:i + size] for i in range(0, len(rows), size)]


async def _scan_chunk(chunk: list[dict[str, Any]], language: str, level: str,
                      llm_conf: dict[str, Any], ui_lang: str) -> dict[str, Any]:
    """扫一块，返回这一块的原始发现。失败抛 LLMError 由上层决定跳过。"""
    lang = get_language(language)
    ui_name = _ui_lang_name(ui_lang)
    convo = "\n".join(
        f"{'Learner' if t.get('role') == 'user' else 'Tutor'}: {t.get('text', '')}"
        for t in chunk)
    text = await chat(
        [{"role": "system", "content": SCAN_SYSTEM.format(ui_language=ui_name)},
         {"role": "user", "content":
          f"Target language: {lang.get('label')}   Learner level: {level}\n"
          f"Learner's native language (write explanations in it): {ui_name}\n\n"
          f"Transcript chunk ({len(chunk)} lines):\n{convo}\n\n"
          f"{SCAN_HINT.replace('<<UI>>', ui_name)}"}],
        base_url=llm_conf.get("base_url", ""),
        api_key=llm_conf.get("api_key", ""),
        model=llm_conf.get("model", "deepseek-chat"),
        temperature=0.1,
        max_tokens=1600,
    )
    raw = _extract_json(text) or {}
    if not isinstance(raw, dict):
        raw = {}
    return raw


def _merge_chunks(scans: list[dict[str, Any]]) -> dict[str, Any]:
    """把各块的发现合并去重（同一个说法在多块里出现的话只留一次）。"""
    seen: set[str] = set()
    errors: list[dict[str, Any]] = []
    phrases: list[str] = []
    topics: list[str] = []
    for sc in scans:
        for e in (sc.get("errors") or [])[:20]:
            if not isinstance(e, dict):
                continue
            said = str(e.get("said") or "").strip()
            key = said.lower()
            if not said or key in seen:
                continue
            seen.add(key)
            errors.append({"you_said": said, "better": str(e.get("better") or "").strip(),
                           "why": str(e.get("why") or "").strip(),
                           "type": str(e.get("type") or "grammar")})
        for p in (sc.get("phrases") or []):
            p = str(p).strip()
            if p and p.lower() not in {x.lower() for x in phrases}:
                phrases.append(p)
        for t in (sc.get("topics") or []):
            t = str(t).strip().lower()
            if t and t not in topics:
                topics.append(t)
    return {"errors": errors[:60], "phrases": phrases[:40], "topics": topics[:12],
            "chunks": len(scans)}


def _build_messages(turns: list[dict[str, Any]], language: str, level: str,
                    ui_lang: str = "zh",
                    digest: dict[str, Any] | None = None,
                    meta: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """拼最终复盘 prompt。

    digest 是前面逐块扫出来的发现（见 _scan_chunk）：把它和全文一起给模型，
    长通话才不会「只看开头」。transcript 仍然全量给（模型要核对原话），
    但带上块数说明，让它知道这是一通被完整看过的电话。
    """
    lang = get_language(language)
    ui_name = _ui_lang_name(ui_lang)
    lines = []
    for t in turns:
        who = "Learner" if t.get("role") == "user" else "Tutor"
        lines.append(f"{who}: {t.get('text', '')}")
    convo = "\n".join(lines)

    parts = [
        f"Target language: {lang.get('label')}   Learner level (estimated): {level}",
        f"Learner's native language (write all human-readable fields in it): {ui_name}",
    ]
    if meta:
        if meta.get("continuation"):
            parts.append(
                "NOTE: this is the continuation of an earlier call. The learner's level, "
                "history and recurring mistakes were already established there. Only "
                "review the lines below; do not re-assess from scratch.")
        if meta.get("includes_parent"):
            parts.append(
                "NOTE: the earlier part of this conversation never got its own review, so "
                "its transcript is included ABOVE (before the continuation). Review the "
                "whole thing as one call.")
        if meta.get("total_turns"):
            parts.append(f"The full call is {meta['total_turns']} lines "
                         f"({meta.get('minutes', 0)} min of speech). Every line is included below.")
    if meta and meta.get("rules_block"):
        parts.append(meta["rules_block"])
    if digest and digest.get("prefindings"):
        d = digest["prefindings"]
        if d.get("errors"):
            lines_e = "\n".join(
                f"- {e['you_said']} => {e['better']}"
                + (f"  ({e['why']})" if e.get("why") else "")
                for e in d["errors"][:40])
            parts.append("Pre-scan of this call (every chunk was read; candidates NOT yet "
                         "filtered):\n" + lines_e)
        if d.get("phrases"):
            parts.append("Expressions that came up: " + ", ".join(d["phrases"][:30]))
        if d.get("topics"):
            parts.append("Topics actually discussed: " + ", ".join(d["topics"][:12]))
        parts.append("Use the pre-scan as evidence, but write your review from the FULL "
                     "transcript. Do not invent anything outside it.")
    parts.append(f"Transcript:\n{convo}")
    parts.append(SCHEMA_HINT.replace("<<UI>>", ui_name))
    user = "\n\n".join(parts)
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

    # ---- 覆盖范围：续接通话只复盘本次新增的内容 ------------------------------
    # 从上一通「继续」出来的这通，turns 只有本次新说的；上一通已经出过复盘，
    # 再把它的内容算进来会让同一个错误在错题本里重复计数，也让评分失真。
    meta = row.get("meta") or {}
    parent_id = meta.get("last_call") or ""
    scope_note = ""
    # 只有「这通是本次新开的一通」时才算续接。切语速的重连会复用同一个 session_id
    # （parent_session == 自己），那种情况必须整通复盘，否则会把自己一半的内容当成
    # 上一通的内容跳过。
    if parent_id and parent_id != session_id and not db.get_report(parent_id):
        # 上一通还没来得及复盘（自动复盘失败/根本没生成）：那本通得把它一起复盘，
        # 否则那段内容永远没人看。接上父通话的字幕，按时间顺序放前面。
        parent = db.get_session(parent_id, user)
        if parent and parent.get("turns"):
            turns = list(parent["turns"]) + list(turns)
            scope_note = "continued"
            log.info("复盘 %s 连带上一通 %s 的字幕（上一通没有复盘）", session_id, parent_id)

    chunks = split_chunks(turns)[:MAX_CHUNKS]
    prefindings: dict[str, Any] = {}
    if len(chunks) > 1:
        # 长通话：逐块扫一遍再汇总。单块（≤60 句）不必多花一次调用。
        scans = []
        for i, ch in enumerate(chunks):
            try:
                scans.append(await _scan_chunk(ch, row.get("language") or "en",
                                               row.get("level") or "B1", llm_conf, ui_lang))
            except LLMError as e:
                # 某一块失败不能让整份复盘失败：跳过它并记日志
                log.warning("复盘分块 %d/%d 扫描失败：%s", i + 1, len(chunks), e)
        prefindings = _merge_chunks(scans)
        log.info("复盘 %s 分块扫描 %d/%d 块，候选错误 %d 条", session_id,
                 len(scans), len(chunks), len(prefindings.get("errors") or []))

    # 用户删过的同类错题 → 已学到的规则：进 prompt，让模型一开始就别记这类问题
    rules_block = mistake_rules.rules_prompt_block(user)

    minutes = round((row.get("duration_ms") or 0) / 60000)
    messages = _build_messages(turns, row.get("language") or "en",
                              row.get("level") or "B1", ui_lang=ui_lang,
                              digest={"prefindings": prefindings} if prefindings else None,
                              meta={"continuation": bool(parent_id) and parent_id != session_id,
                                    "includes_parent": scope_note == "continued",
                                    "rules_block": rules_block,
                                    "total_turns": len([t for t in turns
                                                        if (t.get("text") or "").strip()]),
                                    "minutes": minutes})
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
    # prompt 求过一遍还不够：模型在长转录里常常照记。这里按规则做一次硬过滤，
    # 被剔掉的照样入库（hidden=1 + rule_id），用户在错题本里能恢复。
    kept, dropped = mistake_rules.filter_errors(user, payload["errors"])
    if dropped:
        payload["filtered_by_rules"] = [
            {"you_said": e.get("you_said"), "rule_id": rid} for e, rid in dropped]
        payload["errors"] = kept
    payload["generated_at"] = time.time()
    payload["model"] = llm_conf.get("model", "")
    payload["ui_lang"] = ui_lang      # 这份解释是用哪种母语写的
    # 复盘覆盖了多少内容：句数 / 分块数 / 是否只复盘了本次新增（续接通话）
    # new_only 只在「自己确实是续接出来的新通话」时为 True
    new_only = bool(meta.get("continuation")) and parent_id not in ("", session_id)
    payload["coverage"] = {
        "turns": len([t for t in turns if (t.get("text") or "").strip()]),
        "chunks": len(chunks),
        "own_turns": len([t for t in (row.get("turns") or []) if (t.get("text") or "").strip()]),
        "includes_parent": scope_note == "continued",
        "new_only": new_only,
    }
    db.save_report(user, session_id, payload)
    db.replace_errors(user, session_id, payload["errors"])
    # 被规则移出的也存下来，只是看不见；删错了可以恢复
    if dropped:
        db.replace_errors(user, session_id, [e for e, _ in dropped],
                          hidden=True, rule_map={id(e): rid for e, rid in dropped})
    # 复盘里的生词顺手进生词本：用户不用再手动收藏一次
    try:
        db.add_words(user, session_id, payload["new_words"], ui_lang=ui_lang)
    except Exception:
        log.exception("写入生词本失败（不影响复盘）")
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