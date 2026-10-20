"""通话：WebSocket 主循环 + 开场白音频解析。

这个模块只管「浏览器 ↔ 供应商」的转发与记账，一行具体供应商逻辑都没有 ——
供应商差异全在 app/providers/ 里。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import auth
from ..languages import get_language, voices_for
from ..paths import GREET_DIR
from ..prompts import build_instructions
from ..providers import (
    AudioOut,
    AssistantText,
    ProviderError,
    ProviderSetupError,
    ResponseDone,
    SessionReady,
    UserSpeechStarted,
    UserText,
    create_provider,
)
from ..scenarios import get_scenario, pick_localized
from ..services import profile as profile_svc
from ..services import review
from ..services.llm import LLMError
from ..store import db
from ..store import settings as store

log = logging.getLogger("sayit.call")

router = APIRouter(tags=["call"])


# ---------------------------------------------------------------------------
# 开场白（模型自己念的录音，见 tools/gen_greetings.py）
# ---------------------------------------------------------------------------
def _sanitize_voice(voice: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", voice or "").strip("-")


def resolve_greeting(language: str, scenario: str, voice: str) -> dict[str, Any]:
    """返回 {text, audio_url, duration}。没有预生成音频时 audio_url 为 None，
    前端退化成文字提示（不播错音色）。"""
    opening = pick_localized(get_scenario(scenario).get("opening", {}), language)
    key = f"{language}__{scenario}__{_sanitize_voice(voice)}"
    index_path = GREET_DIR / "index.json"
    if not index_path.exists():
        return {"text": opening, "audio_url": None, "duration": 0}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return {"text": opening, "audio_url": None, "duration": 0}
    rec = index.get(key)
    if not rec:
        return {"text": opening, "audio_url": None, "duration": 0}
    return {
        "text": rec.get("text") or opening,
        "audio_url": rec.get("file"),
        "duration": rec.get("duration", 0),
    }


@router.get("/api/greeting")
async def greeting(language: str = "en", scenario: str = "free_talk", voice: str = ""):
    return resolve_greeting(language, scenario, voice)


# ---------------------------------------------------------------------------
# 通话主循环
# ---------------------------------------------------------------------------
@router.websocket("/ws/call")
async def ws_call(ws: WebSocket) -> None:
    # 鉴权必须在 accept 之前：未登录直接拒握手，浏览器拿不到任何音频通路
    user_info = auth.user_from_websocket(ws)
    if user_info is None:
        await ws.close(code=4401)   # 自定义码：4401 = 未登录
        log.info("WS 拒绝未登录连接 ip=%s", ws.client.host if ws.client else "?")
        return
    await ws.accept()
    user = str(user_info["username"])
    provider = None
    session_id = ""
    started = time.time()
    try:
        # --- 等 start 指令 -------------------------------------------------
        first = await ws.receive()
        if first.get("text") is None:
            await ws.send_json({"type": "error", "message": "第一帧必须是 start JSON"})
            await ws.close()
            return
        req = json.loads(first["text"])
        if req.get("type") != "start":
            await ws.send_json({"type": "error", "message": "第一帧 type 必须是 start"})
            await ws.close()
            return

        s = store.load_settings(user)
        provider_id = req.get("provider") or s["active"]["provider"]
        lang_code = req.get("language") or s["active"]["language"]
        scenario_id = req.get("scenario") or s["active"]["scenario"]
        level = req.get("level") or s["active"]["level"]
        lang = get_language(lang_code)
        if level not in lang.get("levels", []):
            level = lang.get("default_level", "B1")
        voice = req.get("voice") or ""
        if not voice:
            vs = voices_for(lang_code, provider_id)
            voice = vs[0]["id"] if vs else ""

        conf = store.get_provider_conf(user, provider_id)
        if not conf.get("api_key"):
            await ws.send_json({
                "type": "error", "fatal": True, "code": "no_key",
                "message": f"还没有配置 {conf.get('label') or provider_id} 的 API Key ——"
                           f"请到「设置」里填入你自己的 Key",
            })
            await ws.close()
            return

        extra_rules = req.get("extra_rules") or ""
        # 语速：前端选的档位 > 设置里的默认值。会话级参数，整通电话都固定，
        # 不会因为用户中途说「说慢点」而过一会儿又变回去。
        speed_name = str(req.get("speed") or s.get("ui", {}).get("speech_speed") or "slow")
        from ..store.settings import SPEECH_SPEEDS, speed_value
        if speed_name not in SPEECH_SPEEDS:
            speed_name = "slow"
        speed = speed_value(speed_name)
        try:
            # 长期记忆：水平 / 常犯错 / 最近聊过什么 / 用户自述 → 拼进 prompt
            hint = profile_svc.prompt_hint(user, lang_code, level)
            if hint:
                extra_rules = (extra_rules + "\n\n" + hint).strip()
                log.info("注入画像 %d 字 user=%s", len(hint), user)
        except Exception:
            log.exception("读取画像失败（不影响通话）")

        instructions = build_instructions(
            language_code=lang_code,
            scenario_id=scenario_id,
            level=level,
            assistant_name=req.get("assistant_name") or "",
            extra_rules=extra_rules,
            speed_name=speed_name,
        )

        session_id = db.create_session(
            user, provider=provider_id, language=lang_code, scenario=scenario_id,
            level=level, voice=voice,
            meta={"instructions": instructions[:4000], "speed": speed_name},
        )

        provider = create_provider(provider_id, conf=conf, instructions=instructions,
                                   voice=voice, language_code=lang_code, speed=speed)
        try:
            await provider.connect()
        except ProviderSetupError as e:
            await ws.send_json({"type": "error", "fatal": True, "code": e.code,
                                "message": e.message, "detail": e.detail})
            db.close_session(session_id, 0, {"error": e.message})
            await ws.close()
            return

        greeting_info = resolve_greeting(lang_code, scenario_id, voice)
        await ws.send_json({
            "type": "ready",
            "session_id": session_id,
            "greeting": greeting_info,
            "provider": provider_id,
            "language": lang_code,
            "scenario": scenario_id,
            "level": level,
            "voice": voice,
            "speed": speed_name,
            "audio_in_rate": provider.audio_in_rate,
            "audio_out_rate": provider.audio_out_rate,
        })
        log.info("通话开始 %s user=%s provider=%s lang=%s scenario=%s level=%s voice=%s speed=%s",
                 session_id, user, provider_id, lang_code, scenario_id, level, voice,
                 speed_name)

        await _pump(ws, provider, session_id)
        # 挂断后在后台出复盘，不阻塞用户
        asyncio.create_task(_review_after_call(user, session_id))

    except WebSocketDisconnect:
        log.info("浏览器断开 %s", session_id)
    except Exception as e:
        log.exception("通话异常")
        try:
            await ws.send_json({"type": "error", "message": f"服务端异常：{e}", "fatal": True})
        except Exception:
            pass
    finally:
        if provider is not None:
            try:
                await provider.close()
            except Exception:
                pass
        if session_id:
            dur = int((time.time() - started) * 1000)
            db.close_session(session_id, dur)
            try:
                await ws.send_json({"type": "closed", "session_id": session_id,
                                    "duration_ms": dur})
            except Exception:
                pass


async def _review_after_call(user: str, session_id: str) -> None:
    """后台复盘。失败只记日志 —— 复盘出问题绝不能影响通话本身。"""
    try:
        await asyncio.sleep(1.0)
        await review.generate(user, session_id)
    except (LLMError, ValueError) as e:
        log.info("复盘跳过 %s：%s", session_id, e)
    except Exception:
        log.exception("复盘失败 %s", session_id)


async def _pump(ws: WebSocket, provider: Any, session_id: str) -> None:
    """双向泵：浏览器 ↔ 供应商。任一端结束就收工。

    字幕处理是这里最容易出错的地方，两种语义分开处理：
      · 累积式（豆包的 ASR）：每次下发的都是完整文本，取最长者，别拼接
      · 增量式：直接往后拼
    落库只用 final 的完整文本，避免把中间假设写进历史。
    """
    stop = asyncio.Event()
    state = {"user": "", "assistant": ""}
    user_mode = getattr(provider, "user_text_mode", "incremental")
    asst_mode = getattr(provider, "assistant_text_mode", "incremental")
    turn: dict[str, float | None] = {"user_final_at": None, "latencies": []}
    stats: dict[str, Any] = {"turns": 0, "turn_latencies_ms": []}

    def merge(role: str, cur: str, delta: str) -> str:
        if not delta:
            return cur
        mode = user_mode if role == "user" else asst_mode
        if mode == "cumulative":
            # 最长者胜：两条轨道（小写假设 / 带标点精修）都推进到当前进度
            return delta if len(delta) >= len(cur) else cur
        return cur + delta

    async def from_browser() -> None:
        try:
            while not stop.is_set():
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    stop.set()
                    return
                data = msg.get("bytes")
                text = msg.get("text")
                if data:
                    await provider.send_audio(data)
                    continue
                if not text:
                    continue
                try:
                    ev = json.loads(text)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "stop":
                    stop.set()
                    return
                if t == "interrupt":
                    await provider.interrupt()
                elif t == "mute":
                    await provider.set_mute(bool(ev.get("on")))
                elif t == "text":
                    await provider.send_text(str(ev.get("text") or ""))
        except WebSocketDisconnect:
            stop.set()
        except Exception as e:
            log.info("上行结束: %s: %r", type(e).__name__, e)
            stop.set()

    async def to_browser() -> None:
        try:
            async for ev in provider.recv():
                if stop.is_set():
                    return
                if isinstance(ev, AudioOut):
                    # 首帧音频延迟 = 用户说完到 AI 出声，是通话体验的核心指标
                    if turn["user_final_at"] is not None:
                        lat = int((time.time() - turn["user_final_at"]) * 1000)
                        stats["turn_latencies_ms"].append(lat)
                        turn["user_final_at"] = None
                    await ws.send_json({
                        "type": "audio", "rate": ev.rate,
                        "pcm": base64.b64encode(ev.pcm).decode("ascii")})
                elif isinstance(ev, SessionReady):
                    continue
                elif isinstance(ev, UserSpeechStarted):
                    await ws.send_json({"type": "user_speech_started"})
                elif isinstance(ev, UserText):
                    if ev.final:
                        state["user"] = ev.text or state["user"]
                        turn["user_final_at"] = turn["user_final_at"] or time.time()
                        await ws.send_json({"type": "caption", "role": "user",
                                            "text": state["user"], "final": True})
                        db.add_turn(session_id, "user", state["user"])
                        await ws.send_json({"type": "turn_end", "role": "user"})
                        state["user"] = ""
                    elif ev.text:
                        state["user"] = merge("user", state["user"], ev.text)
                        await ws.send_json({"type": "caption", "role": "user",
                                            "text": state["user"], "final": False})
                elif isinstance(ev, AssistantText):
                    if ev.final:
                        state["assistant"] = ev.text or state["assistant"]
                        await ws.send_json({"type": "caption", "role": "assistant",
                                            "text": state["assistant"], "final": True})
                        db.add_turn(session_id, "assistant", state["assistant"])
                        state["assistant"] = ""
                    elif ev.text:
                        state["assistant"] = merge("assistant", state["assistant"], ev.text)
                        await ws.send_json({"type": "caption", "role": "assistant",
                                            "text": state["assistant"], "final": False})
                elif isinstance(ev, ResponseDone):
                    stats["turns"] += 1
                    await ws.send_json({"type": "turn_end", "role": "assistant"})
                elif isinstance(ev, ProviderError):
                    await ws.send_json({"type": "error", "message": ev.message,
                                        "code": ev.code, "fatal": ev.fatal})
                    if ev.fatal:
                        stop.set()
                        return
        except Exception as e:
            log.info("下行结束: %s", e)
        finally:
            stop.set()

    up = asyncio.create_task(from_browser(), name="up")
    down = asyncio.create_task(to_browser(), name="down")
    try:
        await stop.wait()
        log.info("泵结束 %s", session_id)
    finally:
        for t in (up, down):
            t.cancel()
        # 记录供应商侧统计（含首帧延迟），事后排查延迟/断流时有据可查
        try:
            if getattr(provider, "stats", None):
                log.info("通话统计 %s %s", session_id, provider.stats)
        except Exception:
            pass
