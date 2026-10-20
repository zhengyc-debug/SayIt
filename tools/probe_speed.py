"""探针：验证豆包 session.create 里的 audio.output.speed 到底吃不吃、哪个区间。

正弦波可能被 VAD 当成噪声丢掉，所以这里拿 data/greetings 里模型自己念的
开场白（真实人声）当输入触发一轮回复，再比较不同 speed 下回复音频的长度。
语速越慢，同一句话的音频越长；如果四个值长度几乎一样，说明 speed 没生效。

    env -u PYTHONPATH .venv/Scripts/python.exe tools/probe_speed.py --debug
    env -u PYTHONPATH .venv/Scripts/python.exe tools/probe_speed.py --speeds -50,0,50,100
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from app.languages import default_voice  # noqa: E402
from app.paths import GREET_DIR  # noqa: E402
from app.store import settings as store  # noqa: E402

WS_URL = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
INSTR = ("You are Emma, a warm native English speaker on a phone call. "
         "Answer in one or two short sentences. Speak ONLY English.")


def load_pcm() -> bytes:
    """取一段真实语音，转成 16k 单声道 int16。"""
    wavs = sorted(GREET_DIR.glob("en__free_talk__*.wav"))
    if not wavs:
        raise SystemExit("没有开场白 wav 可用")
    path = wavs[0]
    with wave.open(str(path), "rb") as w:
        ch, sw, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    print(f"输入音频 {path.name} ch={ch} sw={sw} rate={rate} bytes={len(raw)}")
    if sw != 2:
        raise SystemExit("只处理 16bit wav")
    if ch > 1:
        shorts = struct.unpack("<" + "h" * (len(raw) // 2), raw)
        raw = struct.pack("<" + "h" * (len(shorts) // ch), *shorts[::ch])
    if rate != 16000:
        n = len(raw) // 2
        src = struct.unpack("<" + "h" * n, raw)
        m = int(n * 16000 / rate)
        out = [src[min(n - 1, int(i * rate / 16000))] for i in range(m)]
        raw = struct.pack("<" + "h" * len(out), *out)
    return raw


async def run(api_key: str, voice: str, speed: float, pcm: bytes, debug: bool) -> dict:
    headers = {"X-Api-Key": api_key}
    async with websockets.connect(WS_URL, additional_headers=headers,
                                  max_size=None, ping_interval=None) as ws:
        await ws.send(json.dumps({
            "type": "session.create",
            "session": {
                "model": "1.2.6.1",
                "instructions": INSTR,
                "audio": {
                    "input": {"format": {"type": "pcm", "rate": 16000}},
                    "output": {"format": {"type": "pcm_s16le", "rate": 24000},
                               "voice": voice, "speed": speed},
                },
                "extension": {"asr": {}, "tts": {}, "dialog": {}},
            },
        }))

        t0 = time.time()
        evs: list[str] = []
        session_ev: dict = {}
        while time.time() - t0 < 15:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            ev = json.loads(raw)
            evs.append(str(ev.get("type")))
            if ev.get("type") in ("session.created", "session.updated"):
                session_ev = ev
                break
            if ev.get("type") == "error":
                return {"speed": speed, "fatal": ev}
        if not session_ev:
            return {"speed": speed, "error": "no session.created", "events": evs}
        if debug:
            sess = session_ev.get("session") or {}
            print("  session:",
                  json.dumps({k: sess.get(k) for k in ("id", "audio", "model")},
                             ensure_ascii=False)[:600])

        # 按 20ms 节奏推真实语音，最多 3 秒，再补 600ms 静音帮 VAD 判定句末
        step = 640
        sent = 0
        for i in range(0, len(pcm), step):
            pkt = pcm[i:i + step]
            if len(pkt) < step:
                break
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(pkt).decode()}))
            sent += step
            await asyncio.sleep(0.02)
            if sent >= 16000 * 2 * 2:
                break
        for _ in range(30):
            await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                      "audio": base64.b64encode(b"\x00" * 640).decode()}))
            await asyncio.sleep(0.02)

        audio_bytes = 0
        first_at = last_at = None
        text = ""
        types_seen: dict[str, int] = {}
        deadline = time.time() + 40
        last_rx = time.time()
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                # 保活：静音期间也要持续送音频，否则服务端报 AudioTTSIdleTimeoutError
                if time.time() - last_rx > 1.0:
                    await ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(b"\x00" * 640).decode()}))
                continue
            last_rx = time.time()
            ev = json.loads(raw)
            t = str(ev.get("type") or "")
            types_seen[t] = types_seen.get(t, 0) + 1
            if debug and t != "response.output_audio.delta":
                # 完整打出来：错误详情（codes=...）和 speed 相关的回执都在原始字段里
                print("   ", t, json.dumps(ev, ensure_ascii=False)[:900])
            if t == "error":
                return {"speed": speed, "error": ev,
                        "audio_sec": round(audio_bytes / 2 / 24000, 2), "types": types_seen}
            if t == "response.output_text.delta":
                text += str(ev.get("delta") or "")
            if t == "response.output_text.done" and ev.get("text"):
                text = str(ev["text"])
            if t == "response.output_audio.delta":
                d = ev.get("delta")
                n = len(base64.b64decode(d)) if d else 0
                if n:
                    if first_at is None:
                        first_at = time.time()
                    last_at = time.time()
                    audio_bytes += n
            if t in ("response.output_audio.done", "response.done") and audio_bytes:
                break
        dur = (last_at - first_at) if (first_at and last_at) else 0
        res = {"speed": speed, "audio_sec": round(audio_bytes / 2 / 24000, 2),
               "wall_sec": round(dur, 2), "text": text[:100]}
        if debug:
            res["types"] = types_seen
        try:
            await ws.send(json.dumps({"type": "session.close"}))
            await asyncio.sleep(0.4)
        except Exception:
            pass
        return res


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speeds", default="-50,0,50,100")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    s = store.load_settings(store.LEGACY_OWNER)
    conf = s["providers"]["doubao"]
    key = conf.get("api_key") or ""
    voice = conf.get("voice") or default_voice("en", "doubao")
    if not key:
        raise SystemExit("没配豆包 Key")
    pcm = load_pcm()
    print(f"voice={voice}")
    for raw_speed in a.speeds.split(","):
        speed = float(raw_speed)
        try:
            r = await run(key, voice, speed, pcm, a.debug)
        except Exception as e:
            r = {"speed": speed, "exception": f"{type(e).__name__}: {e}"}
        print(json.dumps(r, ensure_ascii=False))
        await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
