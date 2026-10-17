"""Stage 0 验证脚本 —— 不确定性的「清零器」。

它做的事：
  1. 连上配置里的供应商（默认豆包）
  2. 把一段英文 wav 按 20ms 节奏流式送进去
  3. 把 AI 回过来的音频存成 wav（可以直接双击听）
  4. 打印：连接耗时、首字延迟、首帧音频延迟、逐句字幕、错误

用法（在 E:\\SayIt 下）：
    python tools/stage0_test.py                       # 用 tmp/hello.wav
    python tools/stage0_test.py path/to/other.wav
    python tools/stage0_test.py --provider openai_realtime
    python tools/stage0_test.py --keep-open 10        # 送完音频再等 10 秒

验收标准：能听到 AI 用英文回答，且"首帧音频延迟" < 1500ms。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_provider_conf, load_settings, RECORD_DIR  # noqa: E402
from app.languages import get_language, voices_for  # noqa: E402
from app.prompts import build_instructions  # noqa: E402
from app.providers import (  # noqa: E402
    AudioOut, AssistantText, ProviderError, ProviderSetupError,
    ResponseDone, SessionReady, UserSpeechStarted, UserText, create_provider,
)

FFMPEG = "ffmpeg"


def to_pcm16k_mono(src: Path) -> bytes:
    """任意音频 → PCM16 / 单声道 / 16k 的裸字节。优先 ffmpeg，退化到 wave 模块。"""
    try:
        out = subprocess.run(
            [FFMPEG, "-v", "error", "-i", str(src), "-f", "s16le",
             "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", "-"],
            capture_output=True, check=True,
        ).stdout
        if out:
            return out
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    with wave.open(str(src), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(
                f"没有 ffmpeg，且 {src.name} 不是 16k/单声道/16bit wav。"
                "装个 ffmpeg 或用 Audacity 转一下。")
        return w.readframes(w.getnframes())


def write_wav(pcm: bytes, path: Path, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", default=str(ROOT / "tmp" / "hello.wav"))
    ap.add_argument("--provider", default=None)
    ap.add_argument("--language", default=None)
    ap.add_argument("--scenario", default="free_talk")
    ap.add_argument("--level", default="B1")
    ap.add_argument("--voice", default=None)
    ap.add_argument("--keep-open", type=float, default=8.0)
    args = ap.parse_args()

    s = load_settings()
    pid = args.provider or s["active"]["provider"]
    lang_code = args.language or s["active"]["language"]
    voice = args.voice or (voices_for(lang_code, pid) or [{}])[0].get("id", "")

    instructions = build_instructions(lang_code, args.scenario, args.level)
    conf = get_provider_conf(pid)

    print("=" * 68)
    print(f"provider = {pid}    endpoint = {conf.get('endpoint')}")
    print(f"language = {lang_code}  level = {args.level}  voice = {voice}")
    print(f"audio in = {args.wav}")
    print("=" * 68)
    print("\n--- system prompt ---")
    print(instructions[:1200] + ("\n...(截断)" if len(instructions) > 1200 else ""))
    print("--- end prompt ---\n")

    if not conf.get("api_key"):
        print(f"[X] 没有 {pid} 的 API Key。去 /settings 页面填，或写进 .env。")
        return 2

    wav_path = Path(args.wav)
    if not wav_path.exists():
        print(f"[*] {wav_path} 不存在，用 Windows 自带语音合成生成一段测试音频…")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(ROOT / "tools" / "make_test_audio.ps1")],
                       check=False)
    pcm = to_pcm16k_mono(wav_path)
    total_ms = len(pcm) / 32
    print(f"输入音频 {len(pcm)} 字节 = {total_ms:.0f} ms\n")

    prov = create_provider(pid, conf=conf, instructions=instructions,
                           voice=voice, language_code=lang_code)
    out_audio = bytearray()
    out_rate = 24000
    t0 = time.perf_counter()
    first_audio_ms = None
    last_user_final: float | None = None   # 毫秒
    turn_latencies: list[int] = []
    transcript: list[str] = []

    async def reader():
        nonlocal first_audio_ms, out_rate, last_user_final
        async for ev in prov.recv():
            now = (time.perf_counter() - t0) * 1000
            if isinstance(ev, SessionReady):
                print(f"[{now:7.0f}ms] 会话就绪 id={ev.session_id or '(空)'}")
            elif isinstance(ev, UserSpeechStarted):
                print(f"[{now:7.0f}ms] 👂 检测到用户开口")
            elif isinstance(ev, UserText):
                tag = "用户完成" if ev.final else "用户..."
                print(f"[{now:7.0f}ms] {tag}: {ev.text}")
                if ev.final:
                    transcript.append("USER: " + ev.text)
                    if last_user_final is None:
                        last_user_final = now
            elif isinstance(ev, AssistantText):
                tag = "AI 完成  " if ev.final else "AI 说话中"
                print(f"[{now:7.0f}ms] {tag}: {ev.text}")
                if ev.final:
                    transcript.append("AI: " + ev.text)
            elif isinstance(ev, AudioOut):
                if last_user_final is not None:
                    # 这个数才是真实体验：用户说完 → AI 出声
                    turn_latencies.append(int(now - last_user_final))
                    last_user_final = None
                if first_audio_ms is None:
                    first_audio_ms = now
                    print(f"[{now:7.0f}ms] 🔊 第一帧 AI 音频到了（{len(ev.pcm)} 字节）")
                out_rate = ev.rate
                out_audio.extend(ev.pcm)
            elif isinstance(ev, ResponseDone):
                print(f"[{now:7.0f}ms] --- 这一轮说话结束 ---")
            elif isinstance(ev, ProviderError):
                print(f"[{now:7.0f}ms] ❌ {ev.message}  (code={ev.code} fatal={ev.fatal})")
                if ev.fatal:
                    return

    try:
        await prov.connect()
    except ProviderSetupError as e:
        print(f"\n[X] 连接失败：{e.message}")
        if e.code:
            print(f"    code = {e.code}")
        if e.detail:
            print(f"    detail = {e.detail[:400]}")
        return 3

    print(f"[{(time.perf_counter()-t0)*1000:7.0f}ms] ✅ 已连接，开始送音频\n")
    rt = asyncio.create_task(reader())

    chunk = 640
    t_start = time.perf_counter()
    for i in range(0, len(pcm), chunk):
        await prov.send_audio(pcm[i:i + chunk])
        # 严格 20ms 节奏：按"已送出的音频时长"对齐真实时间
        target = t_start + (i + chunk) / 32000.0
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
    print(f"\n[{ (time.perf_counter()-t0)*1000:7.0f}ms] 音频送完（{total_ms:.0f}ms）")

    # 继续送静音，让服务端 VAD 判定"说完了"
    silence_end = time.perf_counter() + 1.0
    while time.perf_counter() < silence_end:
        await prov.send_audio(b"\x00" * chunk)
        await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(asyncio.shield(rt), timeout=args.keep_open)
    except asyncio.TimeoutError:
        pass

    await prov.close()
    rt.cancel()

    print("\n" + "=" * 68)
    print("统计")
    print(f"  连接耗时      : {prov.stats.get('connect_ms', '?')} ms")
    if turn_latencies:
        print(f"  用户说完→AI出声: {turn_latencies[0]} ms  (各轮: {turn_latencies})")
    else:
        print("  用户说完→AI出声: 未测到（没有形成完整轮次）")
    print(f"  AI 音频总时长 : {len(out_audio)/2/out_rate:.1f} s  ({out_rate}Hz)")
    print(f"  provider 事件 : {len(transcript)} 条文本")
    print("=" * 68)

    if out_audio:
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        p = RECORD_DIR / f"stage0_{int(time.time())}.wav"
        write_wav(bytes(out_audio), p, out_rate)
        print(f"\n✅ AI 的语音已保存：{p}\n   直接双击播放，判断音色和回答质量。")
    else:
        print("\n❌ 一帧 AI 音频都没收到。看上面的日志里有没有 error 事件。")
        return 4

    if transcript:
        print("\n--- 对话文本 ---")
        print("\n".join(transcript))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
