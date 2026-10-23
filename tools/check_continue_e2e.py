"""续聊链路 e2e（真实豆包 + 真实 WS）。

用法： python tools/check_continue_e2e.py <parent_session_id> [--port 8791]
先经 HTTP 登录拿 cookie → 连 /ws/call 带 parent_session → 用真人录音驱动一两轮，
把 AI 的回复打出来，人工判断它是不是「接着上文说」而不是重新打招呼。
同时打印新 session 的 meta（last_call / root_id / summary 是否落库）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import wave
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
WAV = ROOT / "data" / "greetings" / "en__free_talk__en-female-dacey-uranus-bigtts.wav"


async def main(parent: str, port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base) as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "123456"})
        print("login:", r.status_code, r.json().get("ok"))
        boot = (await c.get("/api/bootstrap")).json()
        print("call_time_limit_min:", boot.get("call_time_limit_min"))
        # session cookie 是 httpx 的 CookieJar 对象，直接 str() 会丢掉签名后的值
        cookie = "; ".join(f"{c.name}={c.value}" for c in c.cookies.jar)

    with wave.open(str(WAV)) as w:
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
        print("wav:", rate, "Hz", w.getnchannels(), "ch")
    # 豆包要 16k / 单声道 / pcm16：24k 的录音先抽稀，否则被当成噪声丢掉
    if rate != 16000:
        import array
        a = array.array("h")
        a.frombytes(frames)
        step = rate // 16000
        pcm = array.array("h", a[::step]).tobytes()
        print("resampled → 16000 Hz,", len(pcm) // 2, "samples")
    else:
        pcm = frames

    ai_text: list[str] = []
    user_text: list[str] = []
    stop = False
    new_sid = None

    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/call",
                                  additional_headers={"Cookie": cookie},
                                  max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "start", "language": "en", "scenario": "free_talk", "level": "B1",
            "provider": "doubao", "speed": "slow", "parent_session": parent,
        }))
        ready = json.loads(await asyncio.wait_for(ws.recv(), 45))
        print("ready:", {k: ready.get(k) for k in
                         ("type", "session_id", "resumed_from", "speed", "greeting")})
        new_sid = ready.get("session_id")

        async def keepalive() -> None:
            silence = b"\x00" * 640
            while not stop:
                await asyncio.sleep(0.02)
                try:
                    await ws.send(silence)
                except Exception:
                    return

        async def reader() -> None:
            nonlocal stop
            while not stop:
                try:
                    raw = await asyncio.wait_for(ws.recv(), 30)
                except asyncio.TimeoutError:
                    return
                if isinstance(raw, bytes):
                    continue
                ev = json.loads(raw)
                t = ev.get("type")
                if t == "caption" and ev.get("final"):
                    (user_text if ev.get("role") == "user" else ai_text).append(ev.get("text", ""))
                elif t == "error":
                    print("ERR:", ev)
                    stop = True
                elif t == "closed":
                    stop = True

        async def speaker() -> None:
            step = 640
            # 一次送完 2 秒真话，不夹静音包 —— 夹在中间的静音会让 VAD 判定成噪声、
            # 一轮都不触发（这是 check_speed_e2e 踩过的坑）
            for i in range(0, min(len(pcm), 16000 * 2 * 3), step):
                chunk = pcm[i:i + step]
                if len(chunk) < step:
                    chunk += b"\x00" * (step - len(chunk))
                await ws.send(chunk)
                await asyncio.sleep(0.02)
            for _ in range(40):                     # 之后补静音，等模型回话
                await ws.send(b"\x00" * 640)
                await asyncio.sleep(0.02)

        tasks = [asyncio.create_task(f()) for f in (reader,)]
        await speaker()
        await asyncio.sleep(25)
        stop = True
        for t in tasks:
            t.cancel()
        try:
            await ws.send(json.dumps({"type": "stop"}))
        except Exception:
            pass

    print("\nUSER:", user_text[-3:])
    print("AI  :", ai_text[:5])
    await asyncio.sleep(2.5)

    async with httpx.AsyncClient(base_url=base) as c:
        d = (await c.get(f"/api/sessions/{new_sid}")).json()
        meta = d.get("meta") or {}
        print("\n新会话:", new_sid, "| last_call =", meta.get("last_call"),
              "| root_id =", meta.get("root_id"))
        print("instructions 含续聊节:", "Where this conversation already got to"
              in (meta.get("instructions") or ""))
        p = (await c.get(f"/api/sessions/{parent}")).json()
        print("父会话 resumed_count =", (p.get("meta") or {}).get("resumed_count"),
              "| child_ids =", p.get("child_ids"))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = 8791
    for a in sys.argv[1:]:
        if a.startswith("--port"):
            port = int(a.split("=", 1)[1]) if "=" in a else 8791
    asyncio.run(main(args[0], port))
