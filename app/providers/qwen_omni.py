"""Qwen-Omni Realtime · 阿里云百炼 —— 预留适配器（骨架）。

这个类存在的意义是证明"换供应商只改一个文件"这件事是真的：
接口签名、事件、音频约定跟 DoubaoProvider 完全一致，
只要把下面 5 个方法填上，UI 里就能直接选它。

接入清单（按顺序做）：
  1. 百炼控制台开通 Qwen-Omni-Realtime，拿 API Key
  2. 端点：wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=<model>
     请求头 Authorization: Bearer <key>
  3. 与会话握手先发一个 session.update（OpenAI Realtime 风格）
     - 输出音频格式默认 pcm24k，注意百炼要求 model 走 query 参数
  4. 上行音频事件：input_audio_buffer.append（base64 pcm16 16k）
  5. 会话有时长上限（120 分钟），到点服务端会主动关连接，要提前重连
  6. 单测：tools/provider_probe.py --provider qwen_omni
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from .base import (
    Event,
    ProviderCaps,
    ProviderSetupError,
    RealtimeProvider,
)

NOT_IMPLEMENTED_MSG = (
    "Qwen-Omni 适配器还没实现（框架已就位）。"
    "需要先开通阿里云百炼 Qwen-Omni-Realtime 并填 API Key，"
    "然后照着本文件顶部注释填 5 个方法即可。"
)


class QwenOmniProvider(RealtimeProvider):
    provider_id = "qwen_omni"
    implemented = False
    audio_in_rate = 16000
    audio_out_rate = 24000

    @classmethod
    def capabilities(cls) -> dict[str, Any]:
        from ..languages import LANGUAGES

        langs = {
            code: list(lang.get("voices", {}).get(cls.provider_id) or [])
            for code, lang in LANGUAGES.items()
        }
        return ProviderCaps(
            id=cls.provider_id,
            label="Qwen-Omni · 阿里云百炼",
            languages={k: v for k, v in langs.items() if v},
            models=["qwen3.5-omni-plus-realtime", "qwen3.8-omni-flash-realtime"],
            audio_in_rate=16000,
            audio_out_rate=24000,
            supports_interrupt=True,
            supports_text_input=True,
            implemented=False,
            notes="骨架已就位，待实现。适合作为豆包的备份线路。",
        ).to_dict()

    async def connect(self) -> None:
        raise ProviderSetupError(NOT_IMPLEMENTED_MSG, code="not_implemented")

    async def send_audio(self, pcm: bytes) -> None:
        raise ProviderSetupError(NOT_IMPLEMENTED_MSG, code="not_implemented")

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def recv(self) -> AsyncIterator[Event]:
        raise ProviderSetupError(NOT_IMPLEMENTED_MSG, code="not_implemented")
        yield  # pragma: no cover
