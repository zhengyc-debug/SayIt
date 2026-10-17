"""配置中心。

优先级：data/settings.json  >  .env  >  内置默认值

设计要点：网页 UI 上能改的一切都落在 data/settings.json；
.env 只作为"首次启动的种子"和"不想进 UI 的兜底"。
API Key 永远不完整回传给前端（只回 masked 版本）。
"""
from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RECORD_DIR = DATA_DIR / "recordings"
SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "sayit.db"
WEB_DIR = BASE_DIR / "web"

load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------
# 默认配置。新增一个供应商时，在这里加一个 key 即可（providers/__init__.py 里
# 也要注册对应的类，两处都用同一个 id）。
# --------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, Any] = {
    "active": {
        "provider": "doubao",
        "language": "en",
        "scenario": "free_talk",
        "level": "B1",
        "voice": "",
        "assistant_name": "",
    },
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
    },
}

# .env -> settings 的种子映射 (provider_id, field, env_name)
ENV_SEEDS = [
    ("doubao", "api_key", "DOUBAO_API_KEY"),
    ("qwen_omni", "api_key", "QWEN_API_KEY"),
    ("openai_realtime", "api_key", "OPENAI_API_KEY"),
    ("llm", "api_key", "DEEPSEEK_API_KEY"),
]

_lock = threading.RLock()


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _seed_from_env(settings: dict) -> bool:
    """把 .env 里的值填进"用户还没配过"的字段。返回是否有改动。"""
    changed = False
    provs = settings.setdefault("providers", {})
    for pid, field, env_name in ENV_SEEDS:
        val = os.environ.get(env_name, "").strip()
        if not val:
            continue
        # llm 系列不是语音供应商，配置挂在顶层同名 key 下
        holder = settings.setdefault(pid, {}) if pid == "llm" else provs.setdefault(pid, {})
        if not holder.get(field):
            holder[field] = val
            changed = True
    prov = os.environ.get("VOICE_PROVIDER", "").strip()
    if prov and not settings.get("active", {}).get("provider"):
        settings["active"]["provider"] = prov
        changed = True
    return changed


def load_settings() -> dict[str, Any]:
    with _lock:
        raw: dict[str, Any] = {}
        if SETTINGS_PATH.exists():
            try:
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        settings = _deep_merge(DEFAULT_SETTINGS, raw)
        if _seed_from_env(settings):
            save_settings(settings)
        return settings


def save_settings(settings: dict[str, Any]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_PATH)


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """局部更新。providers.<id>.<field> 形式也支持。"""
    with _lock:
        cur = load_settings()
        merged = _deep_merge(cur, patch or {})
        save_settings(merged)
        return merged


def get_provider_conf(provider_id: str) -> dict[str, Any]:
    s = load_settings()
    conf = dict(s["providers"].get(provider_id, {}))
    conf.pop("label", None)
    conf["id"] = provider_id
    return conf


def active_conf() -> dict[str, Any]:
    s = load_settings()
    act = dict(s["active"])
    act["provider_conf"] = get_provider_conf(act["provider"])
    act["ui"] = s.get("ui", {})
    return act


def mask_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


def public_settings() -> dict[str, Any]:
    """给前端的版本：任何 api_key 都必须是掩码。"""
    s = deepcopy(load_settings())
    slots = list(s.get("providers", {}).items())
    if isinstance(s.get("llm"), dict):
        slots.append(("llm", s["llm"]))
    for pid, conf in slots:
        conf["api_key_masked"] = mask_key(conf.get("api_key"))
        conf["api_key_set"] = bool(conf.get("api_key"))
        conf.pop("api_key", None)
    return s


def apply_public_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """前端的写操作：api_key 字段若为空字符串或掩码形态，则视为"不改"。"""
    provs = (patch or {}).get("providers") or {}
    if isinstance((patch or {}).get("llm"), dict):
        conf = patch["llm"]
        key = conf.get("api_key")
        if key is not None and (not key or "*" in key):
            conf.pop("api_key", None)
    for pid, conf in provs.items():
        if not isinstance(conf, dict):
            continue
        key = conf.get("api_key")
        if key is not None:
            if not key or "*" in key:
                conf.pop("api_key", None)
    return update_settings(patch)
