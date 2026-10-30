"""按用户隔离的配置存储。

每个用户的配置存在 sayit.db 的 user_settings 表里（user 主键 + settings JSON 列），
互不可见。API Key 只属于配它的那个账号 —— 服务器上没有任何一份全局 Key。

为什么不存文件：data/users/<名字>/settings.json 那个目录结构在容器里要额外
chown、备份时要连目录树一起拷，而且「加个人」会在磁盘上多留一个目录。
包进同一张表之后，一个 sayit.db 就是全部状态。

老数据不迁移：账号和配置都从零开始，admin 账号由环境变量初始化（见 accounts.py）。
"""
from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from .sqlite_conn import LOCK, _conn

log = logging.getLogger("sayit.settings")

# 老全局配置的归属者。迁移只在目标用户还没有自己的配置时发生一次。
LEGACY_OWNER = "admin"

# 表结构：一个用户一行，settings 是一整份 JSON。
# 单行 JSON 而不是拆成 key-value 表：这份配置是「用户手动调的偏好」，
# 从不按字段查询，拆表只会让读改写变成十几次 SQL。
SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user        TEXT PRIMARY KEY,
    settings    TEXT NOT NULL,
    updated_at  REAL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "active": {
        "provider": "doubao",
        "language": "en",
        "scenario": "free_talk",
        "level": "B1",
        "voice": "",
        "assistant_name": "",
    },
    # 供应商默认值：只有非密钥字段。api_key 一律由用户自己在设置页填。
    "providers": {
        "doubao": {
            "label": "豆包 · 火山引擎",
            "api_key": "",
            "endpoint": "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue",
            "model": "1.2.6.1",
            "voice": "",
            # 实测：填 "pcm" 会拿到 float32（播出来是噪声），必须 pcm_s16le
            "output_format": "pcm_s16le",
        },
        "qwen_omni": {
            "label": "Qwen-Omni · 阿里云百炼",
            "api_key": "",
            "endpoint": "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
            "model": "qwen3.5-omni-plus-realtime",
            "voice": "Cherry",
        },
        "openai_realtime": {
            "label": "OpenAI Realtime",
            "api_key": "",
            "endpoint": "wss://api.openai.com/v1/realtime",
            "model": "gpt-realtime",
            "voice": "marin",
        },
    },
    # 复盘/画像用的文本模型。任何 OpenAI 兼容端点都行。
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "enabled": True,
        "label": "复盘用文本模型",
    },
    "ui": {
        "show_captions": True,
        "show_pinyin_hint": False,
        # 单次通话时长上限（分钟）。这是产品规则，不是用户偏好，
        # 所以它是**服务端常量**（见 CALL_TIME_LIMIT_MIN），不吃用户配置 ——
        # 否则老用户的 settings.json 里存着 15 会一直覆盖掉新默认值。
        # 到点自动挂断后可以在记录里「继续这通电话」，上下文不丢。
        "call_time_limit_min": 60,
        # 语速（决定 AI 说得多快）。豆包 session.create 的 audio.output.speed，
        # 实测取 -50/0/50/100 时同一句话的音频长度约 14s / 8s / 4.5s / 3.6s。
        # 默认 -30：练口语时宁可慢一点听清楚。调了就一直生效，不用每次让 AI"说慢点"。
        "speech_speed": "slow",
        # 词卡数据源：默认只用免费词典（有道 web 词典 → dictionaryapi.dev）。
        # 词典都没收录时是否再用文本模型兜底 —— 关掉就完全不吃 token。
        "word_card_llm": True,
    },
}

# 语速档位：名字 → 各供应商的参数值。前端只认名字，供应商差异留在后端。
# 官方定义（端到端实时语音 3.0）：speed 取 [-50, 100]，-50 = 0.5 倍速，0 = 原速，
# 100 = 2.0 倍速。这是 TTS 侧真正的变速合成（不改音高），不是前端慢放。
SPEECH_SPEEDS: dict[str, dict[str, Any]] = {
    "slowest": {"label": "最慢", "label_en": "Slowest", "speed": -50, "rate": 0.5},
    "slow": {"label": "慢", "label_en": "Slow", "speed": -30, "rate": 0.7},
    "normal": {"label": "正常", "label_en": "Normal", "speed": 0, "rate": 1.0},
    "fast": {"label": "快", "label_en": "Fast", "speed": 50, "rate": 1.5},
}
DEFAULT_SPEED = "slow"

# 单次通话最长多久（分钟）。改这个值就是改所有用户的通话上限，前端没有入口。
# 0 = 不限时。之所以不在 DEFAULT_SETTINGS 之外单独存：前端要能从 settings 里读到，
# 而写入路径（设置页）已经不提供这个字段，所以用户配置里留下的旧值要在这里兜住。
CALL_TIME_LIMIT_MIN = 60


def call_time_limit_min(settings: dict[str, Any]) -> int:
    """取通话时长上限：只认服务端常量，忽略用户配置里的历史值（曾默认 15）。"""
    return int(CALL_TIME_LIMIT_MIN)


def speed_value(name: str | None) -> float:
    """档位名 → 供应商参数值。未知值一律回落到默认档。"""
    rec = SPEECH_SPEEDS.get(str(name or DEFAULT_SPEED))
    if rec is None:
        rec = SPEECH_SPEEDS[DEFAULT_SPEED]
    return float(rec["speed"])


def speed_label(name: str | None) -> str:
    """给 prompt 用的一句话描述（让模型自己的措辞也配合慢速）。"""
    name = name if name in SPEECH_SPEEDS else DEFAULT_SPEED
    return {
        "slowest": "very slowly, with clear separation between words",
        "slow": "slowly and clearly",
        "normal": "at a natural conversational pace",
        "fast": "at a brisk natural pace",
    }[name]

def _key(username: str) -> str:
    """用户名就是主键。只做基本校验 —— 合法性在 accounts.validate_username 里管。"""
    name = (username or "").strip()
    if not name:
        raise ValueError("用户名不能为空")
    return name


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _row(username: str) -> dict[str, Any] | None:
    with LOCK, _conn() as c:
        r = c.execute("SELECT settings FROM user_settings WHERE user=?",
                      (username,)).fetchone()
    if not r:
        return None
    try:
        raw = json.loads(r["settings"])
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def load_settings(username: str) -> dict[str, Any]:
    """读某个用户的配置，缺的字段用默认值补齐。首次访问时顺手落库一份。

    落库的意义：设置页看到的是「磁盘上真实存在的配置」，
    出问题时 `select * from user_settings` 一眼能看到这个人到底存了什么。
    """
    name = _key(username)
    raw = _row(name)
    merged = _deep_merge(DEFAULT_SETTINGS, raw or {})
    if raw is None:
        try:
            save_settings(name, merged)
        except Exception:
            pass
    return merged


def save_settings(username: str, settings: dict[str, Any]) -> None:
    name = _key(username)
    payload = json.dumps(settings, ensure_ascii=False)
    with LOCK, _conn() as c:
        c.execute(
            "INSERT INTO user_settings (user, settings, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(user) DO UPDATE SET settings=excluded.settings,"
            " updated_at=excluded.updated_at",
            (name, payload, time.time()))


def update_settings(username: str, patch: dict[str, Any]) -> dict[str, Any]:
    name = _key(username)
    with LOCK:
        merged = _deep_merge(load_settings(name), patch or {})
        save_settings(name, merged)
        return merged


def get_provider_conf(username: str, provider_id: str) -> dict[str, Any]:
    s = load_settings(username)
    conf = dict(s["providers"].get(provider_id, {}))
    conf.pop("label", None)
    conf["id"] = provider_id
    return conf


def active_conf(username: str) -> dict[str, Any]:
    s = load_settings(username)
    act = dict(s["active"])
    act["provider_conf"] = get_provider_conf(username, act["provider"])
    act["ui"] = s.get("ui", {})
    return act


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


def public_settings(username: str) -> dict[str, Any]:
    """给前端的版本：任何 api_key 都必须是掩码。"""
    s = deepcopy(load_settings(username))
    # 通话时长上限是服务端规则：无论用户配置里存了什么（老版本默认 15），
    # 下发给前端的都只能是常量值。
    s.setdefault("ui", {})["call_time_limit_min"] = call_time_limit_min(s)
    slots = list(s.get("providers", {}).items())
    if isinstance(s.get("llm"), dict):
        slots.append(("llm", s["llm"]))
    for _pid, conf in slots:
        conf["api_key_masked"] = mask_key(conf.get("api_key"))
        conf["api_key_set"] = bool(conf.get("api_key"))
        conf.pop("api_key", None)
    # 语速档位的可选项也下发，前端不硬编码名字/顺序
    s["speech_speeds"] = [
        {"id": k, "label": v["label"], "label_en": v["label_en"], "speed": v["speed"],
         "rate": v.get("rate")}
        for k, v in SPEECH_SPEEDS.items()
    ]
    return s


# 掩码形态：前端把 GET 回来的 api_key 原样 PUT 回来时，值长这样
# （xxxxxxxxxxxx + 「…已配置」）。这种值必须当作「不改」，不能当成新 Key 存进去。
MASK_MARK = "…"


def _looks_masked(key: str) -> bool:
    return bool(key) and (MASK_MARK in key or "已配置" in key or "***" in key or "*" in key)


def apply_public_patch(username: str, patch: dict[str, Any]) -> dict[str, Any]:
    """前端的写操作：api_key 为空串或掩码形态时视为"不改"。"""
    patch = deepcopy(patch or {})
    provs = patch.get("providers") or {}
    if isinstance(patch.get("llm"), dict):
        conf = patch["llm"]
        key = conf.get("api_key")
        if key is not None and (not key or _looks_masked(key)):
            conf.pop("api_key", None)
    for _pid, conf in provs.items():
        if not isinstance(conf, dict):
            continue
        key = conf.get("api_key")
        if key is not None and (not key or _looks_masked(key)):
            conf.pop("api_key", None)
        conf.pop("api_key_masked", None)
        conf.pop("api_key_set", None)
    return update_settings(username, patch)


def init_settings(data_dir: Path) -> int:
    """启动时调一次：建表。不做任何文件迁移（全新环境，配置只存在库里）。

    留着 data_dir 参数只是为了调用点统一，字段没用到。
    """
    from .sqlite_conn import ensure_schema
    ensure_schema()
    del data_dir
    return 0
