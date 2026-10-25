#!/bin/sh
# 容器入口：只做「目录权限 + 提示」，账号初始化交给应用自己。
#
# 为什么不再在这里写 users.csv：
#   账号已经搬到 SQLite（accounts 表），初始化逻辑在 app/store/accounts.py，
#   由服务启动时的 lifespan 执行 —— 那里能直接调哈希函数，也保证
#   「不用 docker 直接 python main.py」时行为完全一致。
#
#   · 设置了 SAYIT_ADMIN_PASSWORD → 首次启动用它建 admin
#   · 没设置 → 随机生成一个，打印到日志（docker compose logs sayit 里看）
#   · 已经有 admin → 什么都不做（绝不重置密码）
#   · 真忘了密码 → SAYIT_ADMIN_PASSWORD=新密码 + SAYIT_ADMIN_PASSWORD_FORCE=1 重启
set -eu

DATA_DIR="${SAYIT_DATA_DIR:-/app/data}"

mkdir -p "$DATA_DIR/users" "$DATA_DIR/greetings" "$DATA_DIR/recordings" /app/log \
    || { echo "[entrypoint] 无法创建 $DATA_DIR（宿主机目录挂载时记得 chown 10001:10001）" >&2; exit 1; }

if [ -n "${SAYIT_ADMIN_PASSWORD:-}" ]; then
    echo "[entrypoint] SAYIT_ADMIN_PASSWORD 已设置（首次启动会用它创建 admin）"
else
    echo "[entrypoint] 未设置 SAYIT_ADMIN_PASSWORD：首次启动会随机生成 admin 密码并打印一次"
fi

if [ ! -f "$DATA_DIR/sayit.db" ]; then
    echo "[entrypoint] $DATA_DIR/sayit.db 不存在，将新建（账号表也在里面）"
fi

exec "$@"
