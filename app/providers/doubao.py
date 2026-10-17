"""豆包 · 火山引擎 —— 端到端实时语音大模型（Seeduplex 3.0，全双工）。

协议要点（跟网上大部分老教程不一样，别照抄 1.2.x 的二进制协议）：
  · WebSocket 纯 JSON 文本帧，一条消息一个事件，用 type 字段路由
  · 鉴权：HTTP 头  X-Api-Key: <控制台 API Key>
  · 端点：wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue
  · 上行音频：input_audio_buffer.append 里的 audio 字段，Base64(PCM16 16k)
  · 下行音频：response.output_audio.delta 里的 delta 字段，Base64(PCM16 24k)
  · 必须严格按 20ms 节奏发，过快过慢服务端都会报错
  · 关麦不发数据会被判超时 → 要发 input_audio_mute.commit
  · 关闭要先 session.close 并等服务端确认，直接断开会报 55000001 ContextCanceled
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

log = logging.getLogger("sayit.doubao")

WS_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"

# 服务端错误码 → 中文人话。碰到没见过的码，把原文透传给前端。
ERROR_HINTS: dict[str, str] = {
    "45000030": "这个 API Key 没有获得语音对话服务的授权：控制台里「豆包端到端实时语音大模型」还没开通或没领试用额度。",
    "45000010": "API Key 无效，或者它不属于当前项目（检查控制台项目是否切到 Default）。",
    "45000003": "超过 10 分钟没有交互，服务端已释放连接。",
    "55000001": "会话被异常取消：关闭连接前必须先发 session.close 并等待服务端确认。",
    "50000000": "模型推理出错（偶发，可以重试）。",
}


def _friendly(code: str, raw: str) -> str:
    hint = ERROR_HINTS.get(str(code))
    if hint:
        return f"{hint}（错误码 {code}：{raw}）"
    return f"豆包返回错误 {code}：{raw}"


class DoubaoProvider(RealtimeProvider):
    provider_id = "doubao"
    implemented = True
    audio_in_rate = 16000
    audio_out_rate = 24000
    # 实测：豆包的 ASR 会同时下发"流式小写假设"和"带标点精修版"，
    # 两条轨道都是累积全文 —— 所以用户字幕要取最长者，不能拼接。
    user_text_mode = "cumulative"
    assistant_text_mode = "incremental"

    def __init__(self, conf: dict[str, Any], instructions: str = "",
                 voice: str = "", language_code: str = "en"):
        super().__init__(conf, instructions, voice, language_code)
        self.url = self.endpoint or WS_URL
        self.model = self.model or "1.2.6.1"
        self._ws: Any = None
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._seen_types: set[str] = set()
        self._t0 = 0.0
        self.session_id = ""
        self._ready = asyncio.Event()
        # 重要：豆包 3.0 的 output.format.type 必须传 "pcm_s16le"。
        # 传 "pcm"（文档示例里的值）服务端会返回 float32，按 int16 播就是白噪声。
        # 这条是实测出来的，不是文档写的。
        self.output_format = str(self.conf.get("output_format") or "pcm_s16le")

    # ------------------------------------------------------------------
    @classmethod
    def capabilities(cls) -> dict[str, Any]:
        from ..languages import LANGUAGES

        langs = {
            code: list(lang.get("voices", {}).get(cls.provider_id) or [])
            for code, lang in LANGUAGES.items()
        }
        return ProviderCaps(
            id=cls.provider_id,
            label="豆包 · 火山引擎",
            languages={k: v for k, v in langs.items() if v},
            models=["1.2.6.1"],
            audio_in_rate=16000,
            audio_out_rate=24000,
            supports_interrupt=True,
            supports_text_input=True,
            implemented=True,
            notes="国内直连，端到端全双工。输出默认是 Ogg Opus，这里强制要 PCM24k。",
        ).to_dict()

    # ------------------------------------------------------------------
    def _session_payload(self) -> dict[str, Any]:
        return {
            "type": "session.create",
            "session": {
                "model": self.model,
                "instructions": self.instructions,
                "audio": {
                    "input": {"format": {"type": "pcm", "rate": self.audio_in_rate}},
                    "output": {
                        "format": {"type": self.output_format, "rate": self.audio_out_rate},
                        "voice": self.voice,
                        "speed": 0,
                        "loudness": 0,
                    },
                },
                "extension": {"asr": {}, "tts": {}, "dialog": {}},
            },
        }

    async def connect(self) -> None:
        if not self.api_key:
            raise ProviderSetupError("还没配置豆包 API Key", code="no_key")
        headers = {"X-Api-Key": self.api_key}
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers=headers,
                max_size=None,
                open_timeout=20,
                close_timeout=5,
                ping_interval=None,  # 由音频流保活，别让自动 ping 干扰
            )
        except InvalidStatus as e:
            resp = e.response
            body = ""
            try:
                body = resp.body.decode("utf-8", "replace")
            except Exception:
                pass
            if isinstance(body, str) and body.startswith("bytearray("):
                body = body[11:-2]
            code = ""
            msg = body
            try:
                parsed = json.loads(body)
                err = parsed.get("error") or {}
                if isinstance(err, dict):
                    code = str(err.get("code") or "")
                    msg = str(err.get("message") or body)
                elif isinstance(parsed.get("error"), str):
                    msg = parsed["error"]
            except Exception:
                pass
            raise ProviderSetupError(
                _friendly(code, msg) if code else f"连接被拒绝（HTTP {resp.status_code}）：{msg}",
                code=code,
                detail=body,
            ) from e
        except Exception as e:  # 网络层
            raise ProviderSetupError(f"连不上豆包服务：{type(e).__name__}: {e}") from e

        self._reader_task = asyncio.create_task(self._reader(), name="doubao-reader")
        self._t0 = asyncio.get_running_loop().time()
        await self._ws.send(json.dumps(self._session_payload(), ensure_ascii=False))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=20)
        except asyncio.TimeoutError:
            raise ProviderSetupError(
                "开会话超时：服务端 20 秒内没有确认 session.create。"
                "检查模型版本号是否正确、instructions 是否超长。"
            )

    # ------------------------------------------------------------------
    async def _reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    # 1.2.x 的二进制协议。当前实现只支持 3.0 的 JSON 帧。
                    log.warning("收到二进制帧（旧协议？）长度=%d，已忽略", len(raw))
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    log.warning("非 JSON 帧: %r", str(raw)[:200])
                    continue
                try:
                    for out in self._translate(ev):
                        await self._queue.put(out)
                except Exception:
                    log.exception("解析下行事件失败: %r", str(raw)[:300])
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            reason = (e.rcvd.reason if e.rcvd else "") or ""
            if not self._closed:
                await self._queue.put(ProviderError(
                    message=f"与服务端的连接断开了（code={code} {reason}）",
                    code=str(code or ""), fatal=True))
        except Exception as e:
            log.exception("reader 崩了")
            if not self._closed:
                await self._queue.put(ProviderError(
                    message=f"接收循环异常：{type(e).__name__}: {e}", fatal=True))
        finally:
            await self._queue.put(None)

    # ------------------------------------------------------------------
    def _translate(self, ev: dict[str, Any]) -> list[Event]:
        t = str(ev.get("type") or "")
        out: list[Event] = []

        if t == "error" or (t == "" and "error" in ev):
            err = ev.get("error") or {}
            if isinstance(err, dict):
                code = str(err.get("code") or "")
                msg = str(err.get("message") or "")
            else:
                code, msg = "", str(err)
            out.append(ProviderError(message=_friendly(code, msg), code=code, fatal=True))
            return out

        if t in ("session.created", "session.updated"):
            sess = ev.get("session") or {}
            self.session_id = str(sess.get("id") or ev.get("session_id") or "")
            self.stats["connect_ms"] = int(
                (asyncio.get_running_loop().time() - self._t0) * 1000)
            self._ready.set()
            out.append(SessionReady(session_id=self.session_id))
            return out

        # 用户开口（打断信号）
        if t in ("conversation.item.input_audio_transcription.started", "input_audio_buffer.speech_started"):
            out.append(UserSpeechStarted())
            return out

        # 用户字幕
        if t == "conversation.item.input_audio_transcription.delta":
            out.append(UserText(text=str(ev.get("delta") or ev.get("text") or ""), final=False))
            return out
        if t == "conversation.item.input_audio_transcription.completed":
            out.append(UserText(text=str(ev.get("transcript") or ev.get("text") or ""), final=True))
            return out
        if t == "conversation.item.input_audio_transcription.failed":
            out.append(ProviderError(message="这一句没听清（ASR 失败）", fatal=False))
            return out

        # AI 字幕
        if t == "response.output_text.delta":
            out.append(AssistantText(text=str(ev.get("delta") or ""), final=False))
            return out
        if t == "response.output_text.done":
            out.append(AssistantText(text=str(ev.get("text") or ""), final=True))
            return out

        # AI 音频
        if t in ("response.output_audio.delta", "response.audio.delta"):
            b64 = ev.get("delta") or ev.get("audio") or ""
            if b64:
                pcm = base64.b64decode(b64)
                if "first_audio_ms" not in self.stats:
                    loop_t = asyncio.get_running_loop().time()
                    self.stats["first_audio_ms"] = int((loop_t - self._t0) * 1000)
                out.append(AudioOut(pcm=pcm, rate=self.audio_out_rate))
            return out

        if t == "response.output_audio.done":
            # 实测：一轮回复总是以这个事件收尾，response.done 不一定下发
            out.append(ResponseDone())
            return out
        if t == "response.done":
            out.append(ResponseDone())
            return out

        # 兼容 1.2.x 的旧事件名（万一服务端还回旧格式）
        if t in ("ASRInfo",):
            out.append(UserSpeechStarted())
            return out
        if t in ("ASRResponse",):
            res = ev.get("payload_msg") or ev
            out.append(UserText(text=str(res.get("result", {}).get("text", "")
                                         if isinstance(res.get("result"), dict) else ""), final=False))
            return out
        if t in ("ASREnded",):
            res = ev.get("payload_msg") or {}
            txt = ""
            if isinstance(res.get("result"), dict):
                txt = str(res["result"].get("text") or "")
            out.append(UserText(text=txt, final=True))
            return out

        # 其它事件只记录一次，便于发现协议变动
        if t not in self._seen_types:
            self._seen_types.add(t)
            log.info("未处理的下行事件类型: %s | %s", t, json.dumps(ev, ensure_ascii=False)[:300])
        return out

    # ------------------------------------------------------------------
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
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }))

    async def interrupt(self) -> None:
        if self._ws is None or self._closed:
            return
        await self._ws.send(json.dumps({"type": "response.cancel"}))

    async def set_mute(self, muted: bool) -> None:
        if self._ws is None or self._closed:
            return
        await self._ws.send(json.dumps({
            "type": "input_audio_mute.commit" if muted else "input_audio_unmute.commit"
        }))

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
                await self._ws.send(json.dumps({"type": "session.close"}))
                # 等服务端确认（最多 3 秒），避免 55000001 ContextCanceled
                deadline = asyncio.get_running_loop().time() + 3
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        raw = await asyncio.wait_for(self._ws.recv(), timeout=1)
                    except (asyncio.TimeoutError, ConnectionClosed):
                        break
                    try:
                        if json.loads(raw).get("type") in ("session.closed", "session.finished"):
                            break
                    except Exception:
                        break
            except (ConnectionClosed, Exception):
                pass
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
