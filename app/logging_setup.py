"""日志配置。

设计要点：
  1. 日志同时进控制台和文件，文件落在项目根的 log/ 目录下。
  2. 用 RotatingFileHandler 按大小切分（默认 5MB × 5 份），避免长期跑把盘塞满。
  3. 级别由环境变量 LOG_LEVEL 控制（DEBUG / INFO / WARNING / ERROR），默认 INFO。
     调豆包 WebSocket 问题时用 LOG_LEVEL=DEBUG 起服务即可，不用改代码。
  4. 用 dictConfig 统一配置 root logger，而不是 basicConfig：
     uvicorn（含 --reload 的子进程）在 import 前就配置好了自己的 logger，
     basicConfig 会被静默忽略，日志永远进不了文件。
  5. 必须幂等：uvicorn --reload 和多次 import 都会反复调用 setup_logging()，
     重复加 handler 会让每行日志打印 N 遍，所以先清空再装。
"""
from __future__ import annotations

import logging
import logging.config
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "log"
LOG_FILE = LOG_DIR / "sayit.log"

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_configured = False


def log_level() -> str:
    """从环境变量读级别，非法值一律回落到 INFO。"""
    raw = (os.environ.get("LOG_LEVEL") or "").strip().upper()
    return raw if raw in VALID_LEVELS else "INFO"


def setup_logging(force: bool = False) -> None:
    """配置全局日志。重复调用安全。"""
    global _configured
    if _configured and not force:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = log_level()
    config = {
        "version": 1,
        # True：每次配置都重置已有 logger，保证 handler 不叠加
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {"format": LOG_FORMAT, "datefmt": DATE_FORMAT},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": level,
                "stream": "ext://sys.stderr",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "standard",
                "level": level,
                "filename": str(LOG_FILE),
                "maxBytes": 5 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
                # 延迟打开：某些环境下导入期就建文件会失败
                "delay": True,
            },
        },
        "root": {
            "level": level,
            "handlers": ["console", "file"],
        },
        # uvicorn / websockets 的库内日志也收进同一个文件，否则排查
        # 握手失败、连接断开这类问题只能看到自己打的日志。
        "loggers": {
            "uvicorn": {"level": level, "handlers": ["console", "file"],
                        "propagate": False},
            "uvicorn.error": {"level": level, "handlers": ["console", "file"],
                              "propagate": False},
            "uvicorn.access": {"level": level, "handlers": ["console", "file"],
                               "propagate": False},
            "websockets": {"level": "WARNING", "handlers": ["console", "file"],
                           "propagate": False},
        },
    }

    logging.config.dictConfig(config)
    _configured = True

    logging.getLogger("sayit").info(
        "日志已初始化 | level=%s file=%s", level, LOG_FILE)
