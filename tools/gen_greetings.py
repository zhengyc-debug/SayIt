"""预生成每个「场景 × 语言 × 音色」的开场白音频。

为什么要这个：豆包 duplex 3.0 没有让模型"主动开口"的上行事件
（response.create / say_hello 都会被 45000000 拒掉），模型只在检测到
用户语音后才回应。所以接通瞬间要让 AI 先说话，只能由前端播一段音频。

为了不出现"第一句是一个人，后面换了个声音"，这里就用模型自己把开场白
念一遍录下来 —— 音色、口音、语速和对话时完全一致。

原理：喂一段 1 秒的 "Hi"（Windows TTS 生成）触发模型回应，
配上该场景的 system prompt，模型第一句就会是场景开场白。
把这一轮 AI 音频整段截下来存成 wav。

用法:
    python tools/gen_greetings.py              # 生成全部组合
    python tools/gen_greetings.py --force      # 覆盖已有
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.paths import DATA_DIR, GREET_DIR  # noqa: E402
from app.store import settings as store  # noqa: E402
from app.store.settings import get_provider_conf, load_settings  # noqa: E402
from app.languages import LANGUAGES, voices_for  # noqa: E402
from app.prompts import build_instructions  # noqa: E402
from app.providers import (  # noqa: E402
    AudioOut, AssistantText, ProviderError, ProviderSetupError,
    ResponseDone, UserText, create_provider,
)
from app.scenarios import SCENARIOS, pick_localized  # noqa: E402

INDEX = GREET_DIR / "index.json"


def ensure_trigger_pcm() -> bytes:
    """用 Windows TTS 造一段 'Hi' 的 16k pcm，作为"用户开口"的触发器。"""
    raw = GREET_DIR / "_trigger.raw"
    if raw.exists() and raw.stat().st_size > 4000:
        return raw.read_bytes()
    GREET_DIR.mkdir(parents=True, exist_ok=True)
    ps1 = GREET_DIR / "_trigger.ps1"
    ps1.write_text(
        'Add-Type -AssemblyName System.Speech\n'
        '$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n'
        '$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, '
        '[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, '
        '[System.Speech.AudioFormat.AudioChannel]::Mono)\n'
        f'$s.SetOutputToWaveFile("{(GREET_DIR / "_trigger.wav").as_posix()}", $f)\n'
        '$s.Speak("Hi, hello there.")\n'
        '$s.Dispose()\n', encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ps1)], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(GREET_DIR / "_trigger.wav"),
                    "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
                    str(raw)], check=True, capture_output=True)
    return raw.read_bytes()


async def one(lang_code: str, scenario_id: str, voice: str, provider_id: str,
              conf: dict, trigger: bytes, force: bool) -> dict | None:
    import re as _re
    vkey = _re.sub(r"[^a-zA-Z0-9]+", "-", voice).strip("-")
    key = f"{lang_code}__{scenario_id}__{vkey}"
    wav_path = GREET_DIR / f"{key}.wav"
    if wav_path.exists() and not force:
        print(f"  - 跳过 {key}（已存在，--force 可覆盖）")
        return None

    instructions = build_instructions(lang_code, scenario_id, "B1")
    prov = create_provider(provider_id, conf=conf, instructions=instructions,
                           voice=voice, language_code=lang_code)
    audio = bytearray()
    text = ""

    async def reader():
        nonlocal text
        async for ev in prov.recv():
            if isinstance(ev, AudioOut):
                audio.extend(ev.pcm)
            elif isinstance(ev, AssistantText) and ev.final:
                text = ev.text or text
            elif isinstance(ev, ResponseDone):
                return
            elif isinstance(ev, ProviderError):
                print(f"  ! {ev.message}")
                if ev.fatal:
                    return

    t0 = time.perf_counter()
    await prov.connect()
    rt = asyncio.create_task(reader())

    # 送触发器，严格 20ms 节奏
    for i in range(0, len(trigger), 640):
        await prov.send_audio(trigger[i:i + 640])
        await asyncio.sleep(0.02)
    # 尾部静音，让 VAD 判定说完
    for _ in range(40):
        await prov.send_audio(b"\x00" * 640)
        await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(asyncio.shield(rt), timeout=10)
    except asyncio.TimeoutError:
        pass
    await prov.close()
    rt.cancel()

    if not audio:
        print(f"  ✗ {key} 没收到音频")
        return None

    import wave
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes(audio))

    print(f"  ✓ {key}  {(time.perf_counter()-t0):.1f}s  "
          f"{len(audio)/2/24000:.1f}s  {text[:60]!r}")
    return {"key": key, "language": lang_code, "scenario": scenario_id, "voice": voice,
            "file": f"/greetings/{wav_path.name}", "text": text,
            "duration": round(len(audio) / 2 / 24000, 2)}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--scenario", default=None)
    ap.add_argument("--user", default=store.LEGACY_OWNER,
                    help="用哪个账号的 API Key 生成（默认 admin）")
    args = ap.parse_args()

    # 开场白用哪个账号的 Key：默认 admin（LEGACY_OWNER），可用 --user 指定
    who = args.user
    s = load_settings(who)
    pid = args.provider or s["active"]["provider"]
    conf = get_provider_conf(who, pid)
    if not conf.get("api_key"):
        print(f"[X] {pid} 没有 API Key")
        return 2

    trigger = ensure_trigger_pcm()
    GREET_DIR.mkdir(parents=True, exist_ok=True)

    index: dict[str, dict] = {}
    if INDEX.exists():
        try:
            index = json.loads(INDEX.read_text(encoding="utf-8"))
        except Exception:
            index = {}

    langs = [args.language] if args.language else list(LANGUAGES.keys())
    for lang_code in langs:
        voices = voices_for(lang_code, pid)
        if not voices:
            print(f"跳过 {lang_code}: {pid} 没有该语言的音色")
            continue
        for scenario_id in ([args.scenario] if args.scenario else list(SCENARIOS.keys())):
            for v in voices:
                try:
                    rec = await one(lang_code, scenario_id, v["id"], pid, conf,
                                    trigger, args.force)
                    if rec:
                        index[rec["key"]] = rec
                        INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
                except ProviderSetupError as e:
                    print(f"  [X] {e.message}")
                    return 3
                except Exception as e:
                    print(f"  [X] {type(e).__name__}: {e}")
                await asyncio.sleep(0.5)

    print(f"\n完成，共 {len(index)} 个开场白 → {INDEX}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
