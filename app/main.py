"""SayIt 服务端：FastAPI + WebSocket 网关。

职责边界（很重要）：
   这个文件只做「转发 + 记账 + 配置」，一行具体供应商的代码都不应该有。
   所有供应商差异都在 app/providers/ 里。

浏览器 ↔ 服务端协议
  上行（浏览器 → 服务端）
    文本帧: {"type":"start", "language","scenario","level","voice","provider"}
            {"type":"interrupt"}            打断 AI
            {"type":"mute","on":true/false} 麦克风开关（会被转成供应商事件）
            {"type":"text","text":"..."}    可选：打字代替说话
            {"type":"stop"}                 挂断
    二进制帧: PCM16 小端 / 单声道 / 16000Hz / 640 字节（20ms）
  下行（服务端 → 浏览器）
    {"type":"ready","session_id":...}
    {"type":"audio","pcm":"<base64>","rate":24000}
    {"type":"caption","role":"user|assistant","text":...,"final":bool}
    {"type":"user_speech_started"}         ← 收到就立刻停播（打断）
    {"type":"turn_end"}
    {"type":"error","message":...,"code":...,"fatal":bool}
    {"type":"closed","session_id":...,"duration_ms":...,"turns":N}
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, profile, review
from .config import (
    DATA_DIR,
    WEB_DIR,
    active_conf,
    apply_public_patch,
    get_provider_conf,
    load_settings,
    public_settings,
    save_settings,
)
from .languages import LANGUAGES, get_language, public_languages, voices_for
from .logging_setup import setup_logging
from .prompts import build_instructions, default_name
from .providers import (
    AudioOut,
    AssistantText,
    ProviderError,
    ProviderSetupError,
    ResponseDone,
    SessionReady,
    UserSpeechStarted,
    UserText,
    create_provider,
    public_providers,
)
from .llm import LLMError, ping as llm_ping
from .scenarios import public_scenarios

setup_logging()
log = logging.getLogger("sayit")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    s = load_settings()
    log.info("SayIt 启动 | provider=%s language=%s scenario=%s",
             s["active"]["provider"], s["active"]["language"], s["active"]["scenario"])
    yield


app = FastAPI(title="SayIt", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """开发期禁用静态资源缓存 —— 否则改了 JS/CSS 刷新看不到效果，
    很容易误判成"代码没生效"（踩过一次）。"""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path in ("/", "/settings", "/history", "/stats", "/me"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# 登录门禁：除了登录页本身，所有页面/接口都要求已登录。
# 不用 nginx auth_basic 的原因：那样 WebSocket 拿不到登录态，且浏览器弹窗体验差。
# ---------------------------------------------------------------------------
PUBLIC_PATHS = {
    "/login",          # 登录页
    "/api/login",      # 登录接口
    "/api/auth-state", # 前端判断要不要跳登录页
}
PUBLIC_PREFIXES = ("/static/",)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    user = auth.current_user(request)
    if user is None:
        # 接口返回 401 让前端跳转，页面直接 302 —— 少一次 JS 往返
        if path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return RedirectResponse(url=f"/login?next={path}", status_code=303)

    request.state.user = user
    return await call_next(request)


# 登录态 = 签名 cookie。
# 注意 add_middleware 是 LIFO：最后加的跑在最外层。这里必须放在 require_login
# 之后，否则 SessionMiddleware 会在门禁内侧，request.session 直接取不到。
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.session_secret(),
    session_cookie="sayit_session",
    max_age=auth.COOKIE_MAX_AGE,
    same_site="lax",
    https_only=False,   # 线上走 nginx 的 https，但应用本身只见 http，置 True 会丢 cookie
)


# ===========================================================================
# 配置接口
# ===========================================================================
@app.get("/api/bootstrap")
async def api_bootstrap() -> dict[str, Any]:
    """前端一次性拿全：可选语言 / 场景 / 供应商 / 当前配置。"""
    s = public_settings()
    return {
        "active": s["active"],
        "ui": s["ui"],
        # 每门语言下 AI 的默认名字，前端标题用（settings 里配了 assistant_name 时优先用配置）
        "names": {code: default_name(code) for code in LANGUAGES},
        "languages": public_languages(),
        "scenarios": public_scenarios(),
        "providers": public_providers(),
        "provider_settings": s["providers"],
        "llm": s.get("llm", {}),
    }


@app.get("/api/settings")
async def api_get_settings() -> dict[str, Any]:
    return public_settings()


@app.put("/api/settings")
async def api_put_settings(request: Request) -> dict[str, Any]:
    patch = await request.json()
    apply_public_patch(patch)
    return public_settings()


@app.post("/api/settings/test")
async def api_test_provider(request: Request) -> dict[str, Any]:
    """连一下就断，验证 Key / 服务开通状态 / 端点是否可用。

    UI 上的「测试连接」按钮走这里。失败会把错误码和中文原因一起返回。
    """
    body = await request.json() if await request.body() else {}
    pid = body.get("provider") or load_settings()["active"]["provider"]
    conf = get_provider_conf(pid)
    lang_code = body.get("language") or load_settings()["active"]["language"]
    voice = body.get("voice") or (voices_for(lang_code, pid) or [{}])[0].get("id", "")
    prov = None
    try:
        prov = create_provider(pid, conf=conf,
                              instructions="You are a connectivity test. Say OK and stop.",
                              voice=voice, language_code=lang_code)
        t0 = time.perf_counter()
        await prov.connect()
        ms = int((time.perf_counter() - t0) * 1000)
        return {"ok": True, "provider": pid, "connect_ms": ms,
                "session_id": getattr(prov, "session_id", ""),
                "stats": getattr(prov, "stats", {})}
    except ProviderSetupError as e:
        return JSONResponse(status_code=200, content={
            "ok": False, "provider": pid, "code": e.code,
            "message": e.message, "detail": e.detail})
    except NotImplementedError as e:
        return JSONResponse(status_code=200, content={
            "ok": False, "provider": pid, "code": "not_implemented", "message": str(e)})
    except Exception as e:
        log.exception("测试连接失败")
        return JSONResponse(status_code=200, content={
            "ok": False, "provider": pid, "code": type(e).__name__, "message": str(e)})
    finally:
        if prov is not None:
            try:
                await prov.close()
            except Exception:
                pass


@app.get("/api/auth-state")
async def api_auth_state(request: Request) -> dict[str, Any]:
    """前端启动时问一句「我登录了吗」。未登录也返回 200，不要 401，
    否则前端得靠捕获异常来判断，容易和其它错误混在一起。"""
    user = auth.current_user(request)
    return {"logged_in": user is not None, "user": user}


@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    ip = (request.client.host if request.client else "") or "unknown"
    remaining = auth.lock_remaining(ip)
    if remaining:
        return JSONResponse(status_code=429, content={
            "ok": False, "message": f"密码错误次数过多，请 {remaining // 60 + 1} 分钟后再试"})

    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "")
    password = str(body.get("password") or "")

    user = auth.check_login(username, password)
    if user is None:
        auth.record_fail(ip)
        log.warning("登录失败 user=%r ip=%s", username[:32], ip)
        return JSONResponse(status_code=200, content={
            "ok": False, "message": "用户名或密码不正确"})

    auth.clear_fails(ip)
    auth.login_session(request, user)
    log.info("登录成功 user=%s ip=%s", user["username"], ip)
    return JSONResponse(content={"ok": True, "user": user,
                                 "next": str(body.get("next") or "/")})


@app.post("/api/logout")
async def api_logout(request: Request) -> dict[str, Any]:
    user = auth.current_user(request)
    auth.logout_session(request)
    if user:
        log.info("登出 user=%s", user["username"])
    return {"ok": True}


@app.get("/api/health")
async def api_health() -> dict[str, Any]:
    """顺带把运行环境报出来 —— 排障时第一件要确认的事就是"哪个 python 在跑"。"""
    import platform
    import sys

    s = load_settings()
    return {
        "ok": True,
        "provider": s["active"]["provider"],
        "providers": [p["id"] for p in public_providers()],
        "runtime": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "prefix": sys.prefix,
            "is_venv": sys.prefix != sys.base_prefix,
            "in_project_venv": "sayit\\.venv" in sys.prefix.lower(),
        },
    }


# ===========================================================================
# 复盘报告 / 错题本 / 统计
# ===========================================================================
@app.get("/api/sessions/{session_id}/report")
async def api_get_report(session_id: str, ui_lang: str | None = None) -> dict[str, Any]:
    """取复盘。ui_lang 传了但和缓存里的语言不一致时，视为「还没生成」，
    让前端重新生成一份对应母语的解释（而不是把中文解释硬塞给英文界面）。"""
    rep = db.get_report(session_id)
    if rep and (not ui_lang or (rep.get("ui_lang") or "zh") == ui_lang):
        return {"ok": True, "status": "ready", "report": rep}
    row = db.get_session(session_id)
    if not row:
        raise HTTPException(404, "没有这通电话")
    if not any(t.get("role") == "user" for t in row.get("turns") or []):
        return {"ok": False, "status": "no_speech",
                "message": "这通电话没有你的语音，无法复盘"}
    return {"ok": True, "status": "pending"}


@app.post("/api/sessions/{session_id}/report")
async def api_make_report(session_id: str, request: Request) -> dict[str, Any]:
    body = await request.json() if await request.body() else {}
    body = body or {}
    force = bool(body.get("force"))
    ui_lang = str(body.get("ui_lang") or "zh").lower()
    try:
        rep = await review.generate(session_id, force=force, ui_lang=ui_lang)
        return {"ok": True, "status": "ready", "report": rep}
    except LLMError as e:
        return {"ok": False, "status": "error", "message": str(e), "code": "llm"}
    except ValueError as e:
        return {"ok": False, "status": "error", "message": str(e), "code": "data"}
    except Exception as e:
        log.exception("生成复盘失败")
        return {"ok": False, "status": "error", "message": f"{type(e).__name__}: {e}"}


@app.post("/api/llm/test")
async def api_llm_test() -> dict[str, Any]:
    s = load_settings()["llm"]
    try:
        return {"ok": True, **(await llm_ping(
            base_url=s.get("base_url", ""), api_key=s.get("api_key", ""),
            model=s.get("model", "")))}
    except LLMError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


@app.get("/api/profile")
async def api_profile() -> dict[str, Any]:
    s = active_conf()
    snap = profile.snapshot(s["language"])
    snap["target_level"] = s["level"]
    return snap


@app.put("/api/profile")
async def api_put_profile(request: Request) -> dict[str, Any]:
    body = await request.json()
    db.update_profile_row(display_name=body.get("display_name"),
                          notes=body.get("notes"))
    return await api_profile()


@app.get("/api/errors")
async def api_errors(limit: int = 50) -> dict[str, Any]:
    return {"errors": db.top_errors(limit)}


@app.get("/api/stats")
async def api_stats() -> dict[str, Any]:
    return db.stats_overview()


# ===========================================================================
# 开场白（模型自己念的录音，见 tools/gen_greetings.py）
# ===========================================================================
GREET_DIR = DATA_DIR / "greetings"


def _sanitize_voice(voice: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9]+", "-", voice or "").strip("-")


def resolve_greeting(language: str, scenario: str, voice: str) -> dict[str, Any]:
    """返回 {text, audio_url, duration}。没有预生成音频时 audio_url 为 None，
    前端退化成文字提示（不播错音色）。"""
    from .scenarios import get_scenario, pick_localized

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


@app.get("/api/greeting")
async def api_greeting(language: str = "en", scenario: str = "free_talk", voice: str = ""):
    return resolve_greeting(language, scenario, voice)


# ===========================================================================
# 通话记录
# ===========================================================================
@app.get("/api/sessions")
async def api_sessions(limit: int = 30, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(100, int(limit)))
    offset = max(0, int(offset))
    return {
        "sessions": db.list_sessions(limit, offset),
        "total": db.count_sessions(),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/sessions/{session_id}")
async def api_session_detail(session_id: str) -> dict[str, Any]:
    row = db.get_session(session_id)
    if not row:
        raise HTTPException(404, "没有这通电话")
    return row


# ===========================================================================
# 通话主循环
# ===========================================================================
@app.websocket("/ws/call")
async def ws_call(ws: WebSocket) -> None:
    # 鉴权必须在 accept 之前：未登录直接拒握手，浏览器拿不到任何音频通路
    user = auth.user_from_websocket(ws)
    if user is None:
        await ws.close(code=4401)   # 自定义码：4401 = 未登录
        log.info("WS 拒绝未登录连接 ip=%s", ws.client.host if ws.client else "?")
        return
    await ws.accept()
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

        s = load_settings()
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

        extra_rules = req.get("extra_rules") or ""
        try:
            # 长期记忆：水平 / 常犯错 / 最近聊过什么 / 用户自述 → 拼进 prompt
            hint = profile.prompt_hint(lang_code, level)
            if hint:
                extra_rules = (extra_rules + "\n\n" + hint).strip()
                log.info("注入画像 %d 字", len(hint))
        except Exception:
            log.exception("读取画像失败（不影响通话）")

        instructions = build_instructions(
            language_code=lang_code,
            scenario_id=scenario_id,
            level=level,
            assistant_name=req.get("assistant_name") or "",
            extra_rules=extra_rules,
        )

        session_id = db.create_session(
            provider=provider_id, language=lang_code, scenario=scenario_id,
            level=level, voice=voice,
            meta={"instructions": instructions[:4000]},
        )

        conf = get_provider_conf(provider_id)
        provider = create_provider(provider_id, conf=conf, instructions=instructions,
                                   voice=voice, language_code=lang_code)
        try:
            await provider.connect()
        except ProviderSetupError as e:
            await ws.send_json({"type": "error", "fatal": True, "code": e.code,
                                "message": e.message, "detail": e.detail})
            db.close_session(session_id, 0, {"error": e.message})
            await ws.close()
            return

        greeting = resolve_greeting(lang_code, scenario_id, voice)
        await ws.send_json({
            "type": "ready",
            "session_id": session_id,
            "greeting": greeting,
            "provider": provider_id,
            "language": lang_code,
            "scenario": scenario_id,
            "level": level,
            "voice": voice,
            "audio_in_rate": provider.audio_in_rate,
            "audio_out_rate": provider.audio_out_rate,
        })
        log.info("通话开始 %s provider=%s lang=%s scenario=%s level=%s voice=%s",
                 session_id, provider_id, lang_code, scenario_id, level, voice)

        await _pump(ws, provider, session_id)
        # 挂断后在后台出复盘，不阻塞用户
        asyncio.create_task(_review_after_call(session_id))

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


async def _review_after_call(session_id: str) -> None:
    """后台复盘。失败只记日志 —— 复盘出问题绝不能影响通话本身。"""
    try:
        await asyncio.sleep(1.0)
        await review.generate(session_id)
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
            log.info("上行结束: %s", e)
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
    finally:
        for t in (up, down):
            t.cancel()
        await asyncio.gather(up, down, return_exceptions=True)
        for role in ("user", "assistant"):
            if state[role]:
                db.add_turn(session_id, role, state[role])
        try:
            db.close_session(session_id, 0, {"stats": stats})
        except Exception:
            pass
        log.info("通话结束 %s | 轮数=%s 首帧延迟=%s", session_id, stats["turns"],
                 stats["turn_latencies_ms"])


# ===========================================================================
# 静态页面
# ===========================================================================
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/login")
async def login_page(request: Request):
    """已登录的人再访问登录页就直接送回去，省得误点。"""
    if auth.current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return FileResponse(WEB_DIR / "login.html")


@app.get("/settings")
async def settings_page() -> FileResponse:
    return FileResponse(WEB_DIR / "settings.html")


@app.get("/history")
async def history_page() -> FileResponse:
    return FileResponse(WEB_DIR / "history.html")


@app.get("/stats")
async def stats_page() -> FileResponse:
    return FileResponse(WEB_DIR / "stats.html")


@app.get("/me")
async def me_page() -> FileResponse:
    return FileResponse(WEB_DIR / "me.html")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.webmanifest",
                        media_type="application/manifest+json")


# 静态资源白名单：只放行登录页和公共页面真正需要的文件。
# 不能直接把整个 web/ 挂上去 —— 那样 /static/settings.html、/static/settings.js
# 这些本该登录后才能看的文件会绕过登录门禁（/static/ 在公开前缀里）。
# 用「文件名白名单」而不是「后缀白名单」：settings.js 也是 .js，
# 但只有登录后的设置页才用它，不该公开。
PUBLIC_STATIC = {
    "styles.css",        # 所有页面（含登录页）的样式
    "i18n.js",           # 登录页和全站都用
    "nav.js",            # 底部标签栏（登录页会提前 return，但加载无害）
    "app.js",            # 通话页逻辑
    "audio-worklet.js",  # AudioWorklet，必须固定路径
    "icon.svg",          # favicon / PWA 图标
}


@app.get("/static/{filename:path}", include_in_schema=False)
async def static_file(filename: str) -> FileResponse:
    # 只认平铺文件名，带斜杠或 .. 一律 404（防穿越）
    if filename not in PUBLIC_STATIC:
        raise HTTPException(404)
    target = (WEB_DIR / filename).resolve()
    if not target.is_file():
        raise HTTPException(404)
    return FileResponse(target)


# 开场白音频（模型自己念的录音）
GREET_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/greetings", StaticFiles(directory=str(GREET_DIR)), name="greetings")