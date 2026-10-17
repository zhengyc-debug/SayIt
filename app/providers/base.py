"""供应商抽象层 —— 这是整个项目最重要的一个文件。

一、为什么要有它
   业务代码（FastAPI 网关、前端）只认这里的 5 个方法和 7 种事件，
   永远不 import 任何具体供应商。换供应商 = 新写一个子类 + 改一行配置。

二、一个供应商必须回答的三个问题
   1. capabilities()  —— 你支持哪些语言、哪些音色、哪些模型？（给 UI 用）
   2. connect() 之后我怎么把音频喂给你？(send_audio)
   3. 你会怎么把"字幕 / 音频 / 用户开口了"告诉我？(recv 产出事件)

三、音频约定（全项目统一，适配器负责转换）
   上行：PCM16 小端、单声道、16000Hz、每包 640 字节（20ms）
   下行：PCM16 小端、单声道、24000Hz
   若某供应商只吃别的格式（如 Opus / 16k 输出），在适配器内部转换，
   不要把差异泄漏到外面。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


# ---------------------------------------------------------------------------
# 下行事件。前端网关把每种事件翻译成一条 WebSocket JSON 消息。
# ---------------------------------------------------------------------------
@dataclass
class Event:
    pass


@dataclass
class SessionReady(Event):
    session_id: str = ""


@dataclass
class UserSpeechStarted(Event):
    """用户开口了 —— 前端收到后要立刻停掉正在播放的 AI 语音（打断）。"""
    pass


@dataclass
class UserText(Event):
    text: str = ""
    final: bool = False


@dataclass
class AssistantText(Event):
    text: str = ""
    final: bool = False


@dataclass
class AudioOut(Event):
    pcm: bytes = b""
    rate: int = 24000


@dataclass
class ResponseDone(Event):
    """一轮 AI 回复结束（音频也发完了）。"""
    pass


@dataclass
class ProviderError(Event):
    message: str = ""
    code: str = ""
    fatal: bool = False


# ---------------------------------------------------------------------------
class ProviderSetupError(RuntimeError):
    """连接/鉴权阶段就失败。带 code，方便前端给中文人话提示。"""

    def __init__(self, message: str, code: str = "", detail: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "code": self.code, "detail": self.detail}


@dataclass
class ProviderCaps:
    """给前端渲染配置界面用。"""

    id: str = ""
    label: str = ""
    languages: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    audio_in_rate: int = 16000
    audio_out_rate: int = 24000
    supports_interrupt: bool = True
    supports_text_input: bool = False
    implemented: bool = True
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "languages": self.languages,
            "models": self.models,
            "audio_in_rate": self.audio_in_rate,
            "audio_out_rate": self.audio_out_rate,
            "supports_interrupt": self.supports_interrupt,
            "supports_text_input": self.supports_text_input,
            "implemented": self.implemented,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
class RealtimeProvider(abc.ABC):
    """所有语音供应商的父类。子类必须实现的只有下面这些。"""

    provider_id: str = "base"
    implemented: bool = False

    # 上行/下行采样率（项目统一约定）
    audio_in_rate: int = 16000
    audio_out_rate: int = 24000

    # 字幕语义：
    #   "cumulative"  = 每次下发的都是"到目前为止的完整文本"，取最长者为准
    #   "incremental" = 每次下发的是新增片段，直接拼接
    user_text_mode: str = "incremental"
    assistant_text_mode: str = "incremental"

    def __init__(
        self,
        conf: dict[str, Any],
        instructions: str = "",
        voice: str = "",
        language_code: str = "en",
    ):
        self.conf = conf or {}
        self.instructions = instructions
        self.voice = voice
        self.language_code = language_code
        self.api_key = str(self.conf.get("api_key") or "")
        self.endpoint = str(self.conf.get("endpoint") or "")
        self.model = str(self.conf.get("model") or "")
        self.stats: dict[str, Any] = {}

    # ---- 必须实现 ---------------------------------------------------------
    @classmethod
    @abc.abstractmethod
    def capabilities(cls) -> dict[str, Any]:
        """返回 ProviderCaps.to_dict() 结构。"""

    @abc.abstractmethod
    async def connect(self) -> None:
        """建连 + 开会话。失败请抛 ProviderSetupError。"""

    @abc.abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """上行一包 PCM16 16k（640 字节 / 20ms）。"""

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """让模型立刻停止当前这轮回复。"""

    @abc.abstractmethod
    async def close(self) -> None:
        """优雅关闭（先发结束事件再断 WS）。"""

    @abc.abstractmethod
    def recv(self) -> AsyncIterator[Event]:
        """异步产出下行事件。实现成 async generator。"""

    # ---- 可选实现（默认空实现，不支持的供应商不用管）---------------------
    async def send_text(self, text: str) -> None:
        raise NotImplementedError(f"{self.provider_id} 不支持文本输入")

    async def set_mute(self, muted: bool) -> None:
        """麦克风静音时用。部分供应商（豆包）依赖持续音频流保活，必须覆盖。"""
        return None

    async def ping(self) -> None:
        return None

    # ---- 工具 -------------------------------------------------------------
    @property
    def caps(self) -> dict[str, Any]:
        return self.capabilities()
