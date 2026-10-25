"""全局路径与常量。

所有模块都从这里取路径，不要在别处再算 BASE_DIR —— 免得各处对不齐。

两项可被环境变量覆盖（容器里用；本机不设就是原样）：
  SAYIT_DATA_DIR  数据目录，默认 <项目根>/data
  SAYIT_LOG_DIR   日志目录，默认 <项目根>/log
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("SAYIT_DATA_DIR") or (BASE_DIR / "data")).expanduser()
LOG_DIR = Path(os.environ.get("SAYIT_LOG_DIR") or (BASE_DIR / "log")).expanduser()

# 账号/配置都在 sayit.db 里；没有 data/users/ 目录了
DB_PATH = DATA_DIR / "sayit.db"

WEB_DIR = BASE_DIR / "web"

# 开场白音频：镜像自带一份，启动时缺的会从 DEFAULT_GREET_DIR 补到 DATA_DIR/greetings
DEFAULT_GREET_DIR = BASE_DIR / "data" / "greetings"
GREET_DIR = DATA_DIR / "greetings"
