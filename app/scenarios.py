"""场景注册表 —— 新增场景 = 加一个 dict 条目，不用改代码逻辑。

prompt 按语言 keyed（"en" / "ja" / "*" 兜底），这样同一场景能复用到别的语言。
opening 是 AI 的开场白，会被塞进 system prompt 里让模型先开口。
"""
from __future__ import annotations

from typing import Any

SCENARIOS: dict[str, dict[str, Any]] = {
    "free_talk": {
        "id": "free_talk",
        "icon": "💬",
        "label_zh": "日常闲聊",
        "label": "Free talk",
        "opening": {
            "en": "Hey! Good to hear from you. How's your day going so far?",
            "*": "Hey! Good to hear from you. How's your day going so far?",
        },
        "prompt": {
            "en": (
                "This is a casual phone chat between friends. No topic agenda: "
                "follow whatever the learner brings up, react naturally, share small "
                "opinions of your own, and keep the conversation alive by asking "
                "one light follow-up question at a time."
            ),
            "*": (
                "This is a casual phone chat. Follow the learner's lead, react "
                "naturally, and keep it alive with one light question at a time."
            ),
        },
    },
    "cafe": {
        "id": "cafe",
        "icon": "☕",
        "label_zh": "咖啡店点单",
        "label": "At the café",
        "opening": {
            "en": "Hi there, welcome in! What can I get started for you today?",
            "*": "Hi there, welcome in! What can I get started for you today?",
        },
        "prompt": {
            "en": (
                "You are a barista at a busy café. The learner is the customer. "
                "Stay in character: take the order, offer sizes and milk options, "
                "mention the total, and handle one small realistic complication "
                "(they're out of something, the card reader is slow). Speak like "
                "real café staff: short, friendly, a bit rushed."
            ),
            "*": "You are a barista. The learner is the customer. Stay in character and keep it realistic.",
        },
    },
    "interview": {
        "id": "interview",
        "icon": "💼",
        "label_zh": "面试问答",
        "label": "Job interview",
        "opening": {
            "en": "Thanks for making the time today. To start off, could you tell me a bit about yourself?",
            "*": "Thanks for making the time today. Could you tell me a bit about yourself?",
        },
        "prompt": {
            "en": (
                "You are a hiring manager interviewing the learner for a software "
                "engineering role. Ask one question at a time, follow up on vague "
                "answers with 'can you give me a concrete example?', and keep your "
                "own turns short. Do not give feedback or tips during the interview."
            ),
            "*": "You are a hiring manager. Ask one interview question at a time and keep your turns short.",
        },
    },
    "ielts_p1": {
        "id": "ielts_p1",
        "icon": "🎓",
        "label_zh": "雅思 Part 1",
        "label": "IELTS Speaking Part 1",
        "opening": {
            "en": "Let's begin. Where are you from, and what do you like about living there?",
            "*": "Let's begin. Where are you from, and what do you like about living there?",
        },
        "prompt": {
            "en": (
                "You are an IELTS speaking examiner doing Part 1. Ask short, familiar "
                "questions (home, work, hobbies, food, weather, technology) one at a "
                "time. Do not comment on their answers, do not teach, do not praise. "
                "Just move to the next question like a real examiner."
            ),
            "*": "You are an examiner. Ask short familiar questions one at a time, without commenting.",
        },
    },
}

DEFAULT_SCENARIO = "free_talk"


def get_scenario(sid: str | None) -> dict[str, Any]:
    return SCENARIOS.get(sid or DEFAULT_SCENARIO) or SCENARIOS[DEFAULT_SCENARIO]


def pick_localized(mapping: dict[str, Any], lang_code: str) -> str:
    if not mapping:
        return ""
    return mapping.get(lang_code) or mapping.get("*") or ""


def public_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "id": s["id"],
            "icon": s.get("icon", ""),
            "label_zh": s.get("label_zh", ""),
            "label": s.get("label", ""),
            "languages": ["*"],
        }
        for s in SCENARIOS.values()
    ]
