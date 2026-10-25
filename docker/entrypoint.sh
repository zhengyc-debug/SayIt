#!/bin/sh
# 容器入口：只建目录 + 打一行提示，其它全交给应用。
#
# 为什么不在这里建账号 / 写配置：
#   账号、每人配置、cookie 签名密钥全在 $SAYIT_DATA_DIR/sayit.db 里，
#   初始化写在 app/store/（lifespan 调用），这样「容器跑」和「本机 python main.py」
#   走的是同一段代码，不会有两套行为。
#
# admin 密码来自 docker-compose.yml 的 environment: SAYIT_ADMIN_PASSWORD。
set -eu

DATA_DIR="${SAYIT_DATA_DIR:-/app/data}"
LOG_DIR="${SAYIT_LOG_DIR:-/app/log}"

mkdir -p "$DATA_DIR" "$LOG_DIR" \
    || { echo "[entrypoint] 无法创建 $DATA_DIR / $LOG_DIR（宿主机目录挂载时请 chown 10001:10001）" >&2; exit 1; }

if [ -f "$DATA_DIR/sayit.db" ]; then
    echo "[entrypoint] 使用已有数据库 $DATA_DIR/sayit.db"
else
    echo "[entrypoint] 首次启动：将新建 $DATA_DIR/sayit.db 并用 SAYIT_ADMIN_PASSWORD 创建 admin"
fi

exec "$@"
