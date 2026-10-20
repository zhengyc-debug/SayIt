"""OpenAI Realtime —— 预留适配器（代码已写，未联调）。

保留它是因为它是"标准协议"，很多国内厂商（阿里、月之暗面、MiniMax）都在抄它的
事件结构。把它跑通之后，接其它厂商基本是复制粘贴改端点。

状态：implemented = False（未实测）。前端会显示"未验证"标记。
注意：国内直连需要代理，填好 endpoint 再试。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, AsyncIterator

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

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

log = logging.getLogger("sayit.openai")


class OpenAIRealtimeProvider(RealtimeProvider):
    provider_id = "openai_realtime"
    implemented = False
    audio_in_rate = 16000
    audio_out_rate = 24000

    def __init__(self, conf: dict[str, Any], instructions: str = "",
                 voice: str = "", language_code: str = "en",
                 speed: float | None = None):
        super().__init__(conf, instructions, voice, language_code, speed=speed)
        self.url = self.endpoint or "wss://api.openai.com/v1/realtime"
        self.model = self.model or "gpt-realtime"
        self._ws: Any = None
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._ready = asyncio.Event()
        self.session_id = ""

    @classmethod
    def capabilities(cls) -> dict[str, Any]:
        from ..languages import LANGUAGES

        langs = {
            code: list(lang.get("voices", {}).get(cls.provider_id) or [])
            for code, lang in LANGUAGES.items()
        }
        return ProviderCaps(
            id=cls.provider_id,
            label="OpenAI Realtime",
            languages={k: v for k, v in langs.items() if v},
            models=["gpt-realtime", "gpt-realtime-2.1"],
            audio_in_rate=24000,
            audio_out_rate=24000,
            supports_interrupt=True,
            supports_text_input=True,
            implemented=False,
            notes="英文最地道，但国内需代理。代码未联调，选中后请先点『测试连接』。",
        ).to_dict()

    async def connect(self) -> None:
        if not self.api_key:
            raise ProviderSetupError("还没配置 OpenAI API Key", code="no_key")
        url = f"{self.url}?model={self.model}"
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "OpenAI-Beta": "realtime=v1",
                },
                max_size=None, open_timeout=20, ping_interval=20,
            )
        except InvalidStatus as e:
            body = ""
            try:
                body = e.response.body.decode("utf-8", "replace")
            except Exception:
                pass
            raise ProviderSetupError(
                f"连接被拒绝（HTTP {e.response.status_code}）：{body[:300]}",
                code=str(e.response.status_code), detail=body) from e
        except Exception as e:
            raise ProviderSetupError(f"连不上 OpenAI：{type(e).__name__}: {e}") from e

        self._reader_task = asyncio.create_task(self._reader(), name="openai-reader")
        # speed 用「相对词数」表达：OpenAI 的 realtime 没有 speed 参数，
        # 支持的是 audio.output.speed（0.25~1.5），这里把豆包风格的
        # -50~100 折算成比例，保证切换供应商时用户选的那档听起来一致。
        session: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "instructions": self.instructions,
            "voice": self.voice or "marin",
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "gpt-realtime-whisper"},
            "turn_detection": {"type": "server_vad", "silence_duration_ms": 500},
        }
        if self.speed is not None:
            ratio = 1.0 + float(self.speed) / 100.0 * 0.5    # -50→0.75, 50→1.25
            session["audio"] = {"output": {"speed": max(0.25, min(1.5, ratio))}}
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            raise ProviderSetupError("开会话超时（20s 内没收到 session.updated）")

    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                for out in self._translate(ev):
                    await self._queue.put(out)
        except ConnectionClosed:
            if not self._closed:
                await self._queue.put(ProviderError(message="与 OpenAI 的连接断开了", fatal=True))
        finally:
            await self._queue.put(None)

    def _translate(self, ev: dict[str, Any]) -> list[Event]:
        t = str(ev.get("type") or "")
        if t == "error":
            err = ev.get("error") or {}
            return [ProviderError(message=str(err.get("message") or err), code=str(err.get("code") or ""), fatal=True)]
        if t in ("session.created", "session.updated"):
            self.session_id = str((ev.get("session") or {}).get("id") or "")
            self._ready.set()
            return [SessionReady(session_id=self.session_id)]
        if t == "input_audio_buffer.speech_started":
            return [UserSpeechStarted()]
        if t == "conversation.item.input_audio_transcription.delta":
            return [UserText(text=str(ev.get("delta") or ""), final=False)]
        if t == "conversation.item.input_audio_transcription.completed":
            return [UserText(text=str(ev.get("transcript") or ""), final=True)]
        if t == "response.audio_transcript.delta":
            return [AssistantText(text=str(ev.get("delta") or ""), final=False)]
        if t == "response.audio_transcript.done":
            return [AssistantText(text=str(ev.get("transcript") or ""), final=True)]
        if t == "response.audio.delta":
            b64 = ev.get("delta") or ""
            if b64:
                return [AudioOut(pcm=base64.b64decode(b64), rate=24000)]
            return []
        if t == "response.done":
            return [ResponseDone()]
        return []

    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None or self._closed:
            return
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }))

    async def send_text(self, text: str) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]},
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def interrupt(self) -> None:
        if self._ws is None or self._closed:
            return
        await self._ws.send(json.dumps({"type": "response.cancel"}))

    async def recv(self) -> AsyncIterator[Event]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item
            if isinstance(item, ProviderError) and item.fatal:
                return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
