"""按用户隔离的配置存储。

每个用户的配置落在 data/users/<username>/settings.json，
互不可见。API Key 只属于配它的那个账号——服务器上没有任何一份全局 Key。

老版本是全局单文件 data/settings.json。首次启动时由 migrate_legacy() 把它
归到 LEGACY_OWNER（线上实际使用的账号）名下，不会丢配置。
"""
from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..paths import USERS_DIR

# 老全局配置的归属者。迁移只在目标用户还没有自己的配置时发生一次。
LEGACY_OWNER = "admin"

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
        "call_time_limit_min": 15,
        # 语速（决定 AI 说得多快）。豆包 session.create 的 audio.output.speed，
        # 实测取 -50/0/50/100 时同一句话的音频长度约 14s / 8s / 4.5s / 3.6s。
        # 默认 -30：练口语时宁可慢一点听清楚。调了就一直生效，不用每次让 AI"说慢点"。
        "speech_speed": "slow",
    },
}

# 语速档位：名字 → 各供应商的参数值。前端只认名字，供应商差异留在后端。
SPEECH_SPEEDS: dict[str, dict[str, Any]] = {
    "slowest": {"label": "最慢", "label_en": "Slowest", "speed": -50},
    "slow": {"label": "慢", "label_en": "Slow", "speed": -30},
    "normal": {"label": "正常", "label_en": "Normal", "speed": 0},
    "fast": {"label": "快", "label_en": "Fast", "speed": 50},
}
DEFAULT_SPEED = "slow"


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

_lock = threading.RLock()
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.@-]+")


def _safe(username: str) -> str:
    """用户名当目录名用，必须消毒，否则 ../ 能跳出 data/ 读任意文件。"""
    name = _SAFE_NAME.sub("_", (username or "").strip())
    if not name or name in (".", ".."):
        raise ValueError(f"非法用户名: {username!r}")
    return name


def user_dir(username: str) -> Path:
    return USERS_DIR / _safe(username)


def settings_path(username: str) -> Path:
    return user_dir(username) / "settings.json"


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(username: str) -> dict[str, Any]:
    """读某个用户的配置，缺的字段用默认值补齐。首次访问时顺手落盘一份。"""
    with _lock:
        path = settings_path(username)
        raw: dict[str, Any] = {}
        exists = path.exists()
        if exists:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        merged = _deep_merge(DEFAULT_SETTINGS, raw)
        if not exists:
            try:
                save_settings(username, merged)
            except Exception:
                pass
        return merged


def save_settings(username: str, settings: dict[str, Any]) -> None:
    with _lock:
        path = settings_path(username)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def update_settings(username: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        merged = _deep_merge(load_settings(username), patch or {})
        save_settings(username, merged)
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
    slots = list(s.get("providers", {}).items())
    if isinstance(s.get("llm"), dict):
        slots.append(("llm", s["llm"]))
    for _pid, conf in slots:
        conf["api_key_masked"] = mask_key(conf.get("api_key"))
        conf["api_key_set"] = bool(conf.get("api_key"))
        conf.pop("api_key", None)
    # 语速档位的可选项也下发，前端不硬编码名字/顺序
    s["speech_speeds"] = [
        {"id": k, "label": v["label"], "label_en": v["label_en"], "speed": v["speed"]}
        for k, v in SPEECH_SPEEDS.items()
    ]
    return s


def apply_public_patch(username: str, patch: dict[str, Any]) -> dict[str, Any]:
    """前端的写操作：api_key 为空串或掩码形态时视为"不改"。"""
    patch = deepcopy(patch or {})
    provs = patch.get("providers") or {}
    if isinstance(patch.get("llm"), dict):
        conf = patch["llm"]
        key = conf.get("api_key")
        if key is not None and (not key or "*" in key):
            conf.pop("api_key", None)
    for _pid, conf in provs.items():
        if not isinstance(conf, dict):
            continue
        key = conf.get("api_key")
        if key is not None and (not key or "*" in key):
            conf.pop("api_key", None)
        conf.pop("api_key_masked", None)
        conf.pop("api_key_set", None)
    return update_settings(username, patch)


def migrate_legacy(legacy_path: Path, owner: str = LEGACY_OWNER) -> bool:
    """把老的全局 data/settings.json 归到 owner 名下。返回是否发生了迁移。

    只在 owner 还没有自己的配置时做，避免覆盖用户后来新配的东西。
    老文件改名为 settings.json.migrated，留个后路。
    """
    with _lock:
        if not legacy_path.exists():
            return False
        if settings_path(owner).exists():
            return False
        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict) or not raw:
            return False
        save_settings(owner, _deep_merge(DEFAULT_SETTINGS, raw))
        try:
            legacy_path.rename(legacy_path.with_suffix(".json.migrated"))
        except Exception:
            pass
        return True
