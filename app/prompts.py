"""System prompt 组装。

把「语言设定 + 学员水平 + 场景 + 教学法规则」拼成一段 instructions。
所有供应商拿到的都是同一段文本 —— 换模型不影响教学策略。

要调 AI 的性格/纠错方式，改这里，不要改供应商代码。
"""
from __future__ import annotations

from typing import Any

from .languages import get_language
from .scenarios import get_scenario, pick_localized

DEFAULT_NAMES = {
    "en": ["Emma", "Alex", "Jordan", "Riley"],
    "ja": ["ゆき", "はると"],
}

# ---- 教学法规则（跨语言通用）----------------------------------------------
TEACHING_RULES = """\
## How you talk
- You are on a phone call. Speak like a real person on the phone, not like a \
document. NEVER use lists, bullet points, markdown, emoji, or headings.
- Keep every reply to 1-3 short sentences. Then, most of the time, hand the \
turn back with a question or a short prompt.
- Never mention that you are an AI, a model, or a tutor. Never describe what \
you are doing. Just talk.
- Match your vocabulary and grammar to the learner's level. Do not use words \
far above their level unless you immediately make the meaning obvious from context.
- Speak {pace}. The pacing is already set in the audio engine — never talk about \
it, never promise to change it, never say "I'll slow down". Just speak.

## How you handle mistakes
- Do NOT correct the learner out loud. No "you should say", no "the correct \
form is", no explanations of grammar.
- Instead, recast: use the correct form naturally in your own next sentence, \
as if you were just agreeing with them. Example: learner says "I go to Beijing \
last year" -> you say "Oh nice, you went to Beijing last year? How was it?"
- If their meaning is unclear, ask a short clarifying question instead of \
guessing.
- If they say nothing for a while, gently nudge: "You still there?" or repeat \
your question in simpler words.

## Language
- Speak ONLY {language}. Never switch to another language on your own.
- {native_hint} If the learner seems completely lost after two attempts, you \
may give ONE very short hint in their native language, then immediately \
continue in {language}.
"""


def _pick_name(lang_code: str) -> str:
    names = DEFAULT_NAMES.get(lang_code) or DEFAULT_NAMES["en"]
    # 稳定但不重复：按语言取第一个
    return names[0]


def default_name(lang_code: str) -> str:
    """这一门语言下 AI 的默认名字。给前端显示用（气泡里只有首字母，标题要全名）。"""
    return _pick_name(lang_code)


def build_instructions(
    language_code: str,
    scenario_id: str,
    level: str,
    assistant_name: str = "",
    extra_rules: str = "",
    speed_name: str = "",
) -> str:
    lang = get_language(language_code)
    scenario = get_scenario(scenario_id)
    name = (assistant_name or "").strip() or _pick_name(language_code)
    language_word = lang.get("instruction_word", "English")
    native_hint = lang.get("native_hint", "")
    opening = pick_localized(scenario.get("opening", {}), language_code)
    scenario_prompt = pick_localized(scenario.get("prompt", {}), language_code)

    from .store.settings import speed_label, DEFAULT_SPEED
    pace = speed_label(speed_name or DEFAULT_SPEED)

    parts: list[str] = []
    parts.append(
        f"You are {name}, a warm and easy-going native {language_word} speaker. "
        f"You are having a real-time voice call with a learner whose {language_word} "
        f"is around CEFR level {level}."
    )
    parts.append(TEACHING_RULES.format(language=language_word, native_hint=native_hint,
                                       pace=pace))
    parts.append(f"## The situation\n{scenario_prompt}")
    if opening:
        parts.append(
            "## Opening\n"
            f"The call has just connected. Start speaking FIRST, right away, without "
            f"waiting for the learner. Your first line should be close to: "
            f'"{opening}"'
        )
    if extra_rules.strip():
        parts.append(f"## Extra rules\n{extra_rules.strip()}")
    return "\n\n".join(parts)


def summary_prompt(language_code: str, transcript: list[dict[str, Any]]) -> str:
    """通话结束后让 LLM 出复盘报告用的 prompt。Stage 2 会用到。"""
    lang = get_language(language_code)
    lines = []
    for t in transcript:
        who = "Learner" if t.get("role") == "user" else "Tutor"
        lines.append(f"{who}: {t.get('text', '')}")
    convo = "\n".join(lines) or "(empty)"
    return (
        f"Below is a transcript of a {lang.get('label')} speaking practice call.\n\n"
        f"{convo}\n\n"
        "Produce a review in this exact structure, in Chinese, as compact JSON:\n"
        '{"overall": "<1-2 sentence 总评>", "errors": [{"you_said": "...", '
        '"better": "...", "why": "<中文一句>", "type": "grammar|word|tense|order"}], '
        '"new_words": [{"word": "...", "meaning": "<中文>"}], '
        '"score": {"grammar": 0-10, "fluency": 0-10, "vocabulary": 0-10}}\n'
        "Only list real errors the learner actually made. If none, errors = []."
    )
