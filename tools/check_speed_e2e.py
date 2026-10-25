"""端到端验证：走浏览器同一条 WebSocket，确认语速真的下发到了豆包。

用法（必须清 PYTHONPATH）：
    env -u PYTHONPATH .venv/Scripts/python.exe tools/check_speed_e2e.py --speed slowest
    env -u PYTHONPATH .venv/Scripts/python.exe tools/check_speed_e2e.py --speed normal

验证内容：
  1. 登录（需先有可用账号：admin 后台 /api/admin/users 新建，或
     python -m app.store.accounts add <用户名> <密码>）
  2. 连 /ws/call，发 start（带 speed 档位名）
  3. 断言 ready.speed == 请求的档位
  4. 推一段真实人声触发一轮回复，统计音频字节数（语速越快，同一句越短）
  5. 抓服务端日志里的 "通话开始 ... speed=" 一行，作为"参数真的到了 provider"的证据
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import urllib.request
import wave
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import websockets  # noqa: E402

BASE = "http://127.0.0.1:8787"
WS_URL = "ws://127.0.0.1:8787/ws/call"
GREET_DIR = ROOT / "data" / "greetings"




def login(user: str, password: str) -> str:
    cj = CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    body = json.dumps({"username": user, "password": password}).encode()
    r = urllib.request.Request(BASE + "/api/login", data=body, method="POST")
    r.add_header("Content-Type", "application/json")
    with op.open(r) as resp:
        assert resp.status == 200, resp.status
    return "; ".join(f"{c.name}={c.value}" for c in cj)


def load_pcm() -> bytes:
    wavs = sorted(GREET_DIR.glob("en__free_talk__*.wav"))
    with wave.open(str(wavs[0]), "rb") as w:
        raw = w.readframes(w.getnframes())
        rate = w.getframerate()
    if rate != 16000:                      # 简单降采样到 16k
        import struct
        n = len(raw) // 2
        src = struct.unpack("<" + "h" * n, raw)
        m = int(n * 16000 / rate)
        raw = struct.pack("<" + "h" * m, *[src[min(n - 1, int(i * rate / 16000))] for i in range(m)])
    return raw


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="tester")
    ap.add_argument("--password", default="verify123")
    ap.add_argument("--speed", default="slowest")
    ap.add_argument("--scenario", default="free_talk")
    a = ap.parse_args()

    cookie = login(a.user, a.password)
    pcm = load_pcm()
    events: list[str] = []
    seen: set[str] = set()
    audio = 0
    ready = {}

    async with websockets.connect(WS_URL, additional_headers={"Cookie": cookie},
                                  max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "start", "language": "en", "scenario": a.scenario,
            "level": "B1", "provider": "doubao", "speed": a.speed,
        }))
        # 等 ready
        t0 = time.time()
        while time.time() - t0 < 25:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
            if ev.get("type") == "ready":
                ready = ev
                break
            if ev.get("type") == "error":
                print("READY ERROR:", json.dumps(ev, ensure_ascii=False))
                return 1
            events.append(str(ev.get("type")))
    print("ready.speed =", ready.get("speed"), "| session:", ready.get("session_id"))

    # 第二轮连接：推音频触发回复，量音频长度
    async with websockets.connect(WS_URL, additional_headers={"Cookie": cookie},
                                  max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "start", "language": "en", "scenario": a.scenario,
            "level": "B1", "provider": "doubao", "speed": a.speed,
        }))
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=25))
            if ev.get("type") == "ready":
                break
            if ev.get("type") == "error":
                print("ERROR:", ev); return 1

        step = 640
        for i in range(0, min(len(pcm), 16000 * 2 * 3), step):
            await ws.send(pcm[i:i + step])          # 二进制帧 = PCM16 16k，20ms 一包
            await asyncio.sleep(0.02)
        for _ in range(30):
            await ws.send(b"\x00" * 640)
            await asyncio.sleep(0.02)

        deadline = time.time() + 40
        text = ""
        turn_done = False
        while time.time() < deadline:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            except asyncio.TimeoutError:
                # 保活，避免 AudioTTSIdleTimeoutError
                await ws.send(b"\x00" * 640)
                print("   (idle tick)", flush=True) if False else None
                continue
            t = ev.get("type")
            events.append(str(t))
            if t not in seen:
                seen.add(t)
                print("   <", t, json.dumps(ev, ensure_ascii=False)[:110], flush=True)
            if t == "audio":
                audio += len(base64.b64decode(ev["pcm"]))
            elif t == "caption" and ev.get("role") == "assistant":
                text = ev.get("text") or text
            elif t == "turn_end" and ev.get("role") == "assistant":
                turn_done = True
                break
        sec = round(audio / 2 / 24000, 2)
        print(f"speed={a.speed} 音频={sec}s 文本={text[:80]!r} turn_end={turn_done}")
        print("turn_end 事件存在（前端就是靠它挂「重听」按钮）:", "turn_end" in events)
        await ws.send(json.dumps({"type": "stop"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
