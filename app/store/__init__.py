"""存储层。

所有数据都在一个 SQLite 文件里（$SAYIT_DATA_DIR/sayit.db）：
  · accounts.py  账号
  · settings.py  每人一份的配置（语言/场景/音色/API Key/复盘模型…）
  · db.py        通话记录 / 错题本 / 复盘 / 生词本

本模块只放「启动时的一次性同步动作」，且必须幂等 —— main.py 的 lifespan
和命令行工具（python -m app.store.accounts ...）都会调它。

刻意不读 .env：配置只有两个来源 —— docker compose 的 environment
（或 systemd 的 EnvironmentFile），以及数据库本身。少一个中间层就少一类
「改了没生效」的困惑。
"""
from __future__ import annotations

import logging
import shutil

from .. import paths

log = logging.getLogger("sayit.store")


def ensure_dirs() -> None:
    """建数据目录。用命名卷/绑定挂载时，宿主机目录可能是空的。"""
    for d in (paths.DATA_DIR, paths.GREET_DIR, paths.DATA_DIR / "recordings"):
        d.mkdir(parents=True, exist_ok=True)


def seed_greetings() -> int:
    """把镜像自带的开场白音频补进数据目录（卷是空的时候整份拷过去）。

    为什么不直接读镜像里的那份：用户要换开场白/换音色时改的是数据卷里的文件，
    镜像里那份只当「出厂默认」。已存在的文件不覆盖。
    """
    src_dir, dst_dir = paths.DEFAULT_GREET_DIR, paths.GREET_DIR
    if not src_dir.exists() or src_dir.resolve() == dst_dir.resolve():
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in src_dir.iterdir():
        if not src.is_file():
            continue
        dst = dst_dir / src.name
        if dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            copied += 1
        except Exception:
            log.exception("复制开场白失败：%s", src)
    if copied:
        log.info("已把 %d 个开场白音频复制到 %s", copied, dst_dir)
    return copied
