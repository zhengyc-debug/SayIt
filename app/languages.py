"""语言注册表 —— 新增一门语言只需要在这里加一个条目。

前端 /api/bootstrap 会把这里的内容连同供应商 capabilities 一起下发，
UI 自动渲染语言下拉框；供应商不支持的语言会自动置灰。

新增语言三步：
 1. 在 LANGUAGES 里加条目（含 instruction_word / voices / native_hint）
 2. 在 app/providers/<your>.py 的 capabilities 里声明支持该语言与音色
 3. 在 app/prompts/ 里确认模板对这个语言读起来通顺（模板本身是语言中立的）
"""
from __future__ import annotations

from typing import Any

LANGUAGES: dict[str, dict[str, Any]] = {
    "en": {
        "code": "en",
        "label": "English",
        "label_zh": "英语",
        "instruction_word": "English",
        "native_name": "English",
        "levels": ["A1", "A2", "B1", "B2", "C1", "C2"],
        "default_level": "B1",
        # 该语言的"母语提示"：学员母语，用于兜底解释
        "native_hint": "The learner's native language is Chinese.",
        "voices": {
            "doubao": [
                {"id": "en_female_dacey_uranus_bigtts", "label": "Dacey · 美式女声"},
                {"id": "en_male_tim_uranus_bigtts", "label": "Tim · 美式男声"},
                {"id": "en_female_stokie_uranus_bigtts", "label": "Stokie · 美式女声"},
            ],
            "qwen_omni": [
                {"id": "Cherry", "label": "Cherry · 女声"},
                {"id": "Ethan", "label": "Ethan · 男声"},
            ],
            "openai_realtime": [
                {"id": "marin", "label": "Marin · 女声"},
                {"id": "cedar", "label": "Cedar · 男声"},
            ],
        },
    },
    # ---- 下面是预留位：等你要加其它语言时，把注释去掉并填音色即可 ----
    # "ja": {
    #     "code": "ja", "label": "Japanese", "label_zh": "日语",
    #     "instruction_word": "Japanese", "native_name": "日本語",
    #     "levels": ["N5", "N4", "N3", "N2", "N1"],
    #     "default_level": "N4",
    #     "native_hint": "The learner's native language is Chinese.",
    #     "voices": {"doubao": [], "qwen_omni": [], "openai_realtime": []},
    # },
}

DEFAULT_LANGUAGE = "en"


def get_language(code: str | None) -> dict[str, Any]:
    return LANGUAGES.get(code or DEFAULT_LANGUAGE) or LANGUAGES[DEFAULT_LANGUAGE]


def voices_for(code: str, provider_id: str) -> list[dict[str, str]]:
    lang = get_language(code)
    return list(lang.get("voices", {}).get(provider_id) or [])


def default_voice(code: str, provider_id: str) -> str:
    vs = voices_for(code, provider_id)
    return vs[0]["id"] if vs else ""


def public_languages() -> list[dict[str, Any]]:
    out = []
    for code, lang in LANGUAGES.items():
        item = {k: v for k, v in lang.items() if k != "voices"}
        item["code"] = code
        item["voices"] = lang.get("voices", {})
        out.append(item)
    return out
