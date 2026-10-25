"""SQLite 连接的唯一入口。

单独一个文件是因为：db.py 和 accounts.py 都要连库，而它们互相 import 会成环。
两边都从这里拿 _conn / init_db，谁先启动都行。

并发取舍：业务写入用「进程内锁 + 事务」，读也走同一把锁。
SQLite 的 WAL 已经能让读写并行，这把锁主要是防止同一进程内的写写冲突。
"""
from __future__ import annotations

import sqlite3
import threading

from ..paths import DATA_DIR, DB_PATH

# 所有模块共用这一把锁：web 线程、WebSocket 线程都往同一个库里写
LOCK = threading.RLock()


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def ensure_schema() -> None:
    """建库建表。必须幂等 —— 每次启动都会跑。

    账号表放在这里而不是 db.py：容器里 app 启动前可能有人先跑
    `python -m app.accounts`（命令行加账号），那时也得能建出表来。
    """
    from .accounts import SCHEMA as ACCOUNTS_SCHEMA

    with LOCK, _conn() as c:
        c.executescript(ACCOUNTS_SCHEMA)
