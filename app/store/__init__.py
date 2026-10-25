"""存储层 + 环境变量加载。

.env 的读取刻意放在这里而不是 main.py：命令行工具（python -m app.store.accounts
list）也要读到 .env，否则它在终端里看到的配置和 web 进程不一致，最容易踩的是
SAYIT_ADMIN_PASSWORD。

不覆盖已有环境变量：容器里 environment: 写的值优先于 .env 文件，
这是 docker compose 的默认语义，反了会让人以为改了 compose 却没生效。
"""
from __future__ import annotations

import os
from pathlib import Path

_DOTENV = Path(__file__).resolve().parent.parent.parent / ".env"


def _load_dotenv(path: Path = _DOTENV) -> bool:
    """把 .env 读进 os.environ。不依赖 python-dotenv，免得又多一个硬依赖。"""
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ[key] = value
    return True


_load_dotenv()
