"""极简 OpenAI 兼容 LLM 客户端。

只干一件事：发 messages，拿回文本。任何 OpenAI 兼容端点都能用
（DeepSeek / 火山方舟 / 阿里百炼兼容模式 / OpenAI / 本地 vLLM）。

不引 openai SDK —— 一个 httpx POST 就够了，少一个依赖少一层坑。
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..store import settings as store_settings

log = logging.getLogger("sayit.llm")


class LLMError(RuntimeError):
    pass


def conf_for(username: str) -> dict[str, Any]:
    """取某个用户配的文本模型参数。"""
    return store_settings.load_settings(username).get("llm") or {}


async def chat(
    messages: list[dict[str, str]],
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float = 0.3,
    max_tokens: int = 1500,
    timeout: float = 90.0,
) -> str:
    if not api_key:
        raise LLMError("还没有配置文本模型 API Key —— 请到「设置 → 复盘用文本模型」里填入你自己的 Key")
    base = (base_url or "").rstrip("/")
    if not base:
        raise LLMError("没有配置文本模型 base_url")
    url = f"{base}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise LLMError(f"连不上文本模型 {base}: {type(e).__name__}: {e}") from e

    if r.status_code >= 400:
        body = r.text[:400]
        raise LLMError(f"文本模型返回 HTTP {r.status_code}: {body}")
    try:
        data = r.json()
        return data["choices"][0]["message"]["content"] or ""
    except Exception as e:
        raise LLMError(f"解析模型返回失败: {e}; body={r.text[:300]}") from e


async def ping(*, base_url: str, api_key: str, model: str) -> dict[str, Any]:
    """设置页「测试连接」用：发一个最小请求，返回耗时。"""
    import time
    t0 = time.perf_counter()
    text = await chat(
        [{"role": "user", "content": "Reply with the single word: ok"}],
        base_url=base_url, api_key=api_key, model=model, max_tokens=10, timeout=30,
    )
    return {"ok": True, "ms": int((time.perf_counter() - t0) * 1000), "reply": text.strip()[:40]}
