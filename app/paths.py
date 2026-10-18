"""全局路径与常量。

所有模块都从这里取路径，不要在别处再算 BASE_DIR —— 免得各处对不齐。
"""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
USERS_DIR = DATA_DIR / "users"          # 每个用户一份配置：data/users/<name>/settings.json
RECORD_DIR = DATA_DIR / "recordings"
DB_PATH = DATA_DIR / "sayit.db"
WEB_DIR = BASE_DIR / "web"
GREET_DIR = DATA_DIR / "greetings"
USERS_CSV = DATA_DIR / "users.csv"
SECRET_PATH = DATA_DIR / "secret.key"   # 没配 SECRET_KEY 时的兜底，落盘避免重启掉登录
