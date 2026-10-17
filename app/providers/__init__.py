"""供应商注册表。

新增一个语音供应商：
 1. 新建 app/providers/<name>.py，继承 RealtimeProvider，实现 5 个方法
 2. 在下面 PROVIDERS 里注册
 3. 在 app/config.py 的 DEFAULT_SETTINGS["providers"] 加默认配置
 4. 在 app/languages.py 对应语言的 voices 里填它的音色
完。
"""
from __future__ import annotations

from typing import Any

from .base import (
    AudioOut,
    AssistantText,
    Event,
    ProviderCaps,
    ProviderError,
    ProviderSetupError,
    RealtimeProvider,
    ResponseDone,
    SessionReady,
    UserSpeechStarted,
    UserText,
)
from .doubao import DoubaoProvider
from .openai_realtime import OpenAIRealtimeProvider
from .qwen_omni import QwenOmniProvider

PROVIDERS: dict[str, type[RealtimeProvider]] = {
    DoubaoProvider.provider_id: DoubaoProvider,
    QwenOmniProvider.provider_id: QwenOmniProvider,
    OpenAIRealtimeProvider.provider_id: OpenAIRealtimeProvider,
}

__all__ = [
    "PROVIDERS",
    "RealtimeProvider",
    "ProviderCaps",
    "Event",
    "AudioOut",
    "AssistantText",
    "UserText",
    "UserSpeechStarted",
    "SessionReady",
    "ResponseDone",
    "ProviderError",
    "ProviderSetupError",
    "get_provider_class",
    "create_provider",
    "public_providers",
]


def get_provider_class(provider_id: str) -> type[RealtimeProvider]:
    cls = PROVIDERS.get(provider_id)
    if cls is None:
        raise KeyError(f"未知的语音供应商: {provider_id}")
    return cls


def create_provider(provider_id: str, **kwargs: Any) -> RealtimeProvider:
    return get_provider_class(provider_id)(**kwargs)


def public_providers() -> list[dict[str, Any]]:
    out = []
    for pid, cls in PROVIDERS.items():
        caps = cls.capabilities()
        caps["id"] = pid
        caps["implemented"] = cls.implemented
        out.append(caps)
    return out
