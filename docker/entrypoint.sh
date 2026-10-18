#!/bin/sh
# 容器入口：准备数据目录，然后在没有账号时自动建一个，最后交给 CMD 启动服务。
#
# 为什么需要这一步：
#   data/users.csv（账号文件）不进镜像 —— 密码不该烘进镜像层。
#   但空目录启动会导致「任何人都登录不了」，所以首次启动必须有一个账号。
#
# 首次启动行为：
#   · 若设置了 SAYIT_ADMIN_PASSWORD，用它建 admin 账号
#   · 否则生成一个随机密码，打印到容器日志里（docker logs sayit 能看到）
#
# 想自己维护账号：挂载 data/users.csv 进去，这里就不做任何事。
set -eu

DATA_DIR="${SAYIT_DATA_DIR:-/app/data}"
USERS_CSV="$DATA_DIR/users.csv"

mkdir -p "$DATA_DIR/users" "$DATA_DIR/greetings" "$DATA_DIR/recordings" /app/log

if [ ! -f "$USERS_CSV" ]; then
    echo "[entrypoint] 没有找到 $USERS_CSV，创建首个管理员账号"

    if [ -n "${SAYIT_ADMIN_PASSWORD:-}" ]; then
        PASSWORD="$SAYIT_ADMIN_PASSWORD"
        GENERATED=0
    else
        # 12 位字母数字，避开容易混淆的字符
        PASSWORD=$(tr -dc 'A-HJ-NP-Za-km-z2-9' < /dev/urandom | head -c 12 || true)
        [ -n "$PASSWORD" ] || PASSWORD="sayit$(date +%s)"
        GENERATED=1
    fi

    # 用明文写进去：首次启动时脚本还没法安全地调用应用内的哈希函数，
    # 而 auth.py 本来就支持明文密码列。登录后建议改成哈希（见 README）。
    printf 'username,password,display_name\nadmin,%s,管理员\n' "$PASSWORD" > "$USERS_CSV"
    chmod 600 "$USERS_CSV"

    if [ "$GENERATED" = "1" ]; then
        echo "=============================================================="
        echo "  已创建管理员账号"
        echo "    用户名: admin"
        echo "    密码:   $PASSWORD"
        echo "  这个密码只显示这一次，请立即记录并登录后修改。"
        echo "  想指定密码：设置环境变量 SAYIT_ADMIN_PASSWORD 后重新以空卷启动。"
        echo "=============================================================="
    else
        echo "[entrypoint] 已用 SAYIT_ADMIN_PASSWORD 创建 admin 账号"
    fi
fi

# SECRET_KEY 落盘位置（没配环境变量时 auth.py 会自动生成到这里）
if [ -f "$DATA_DIR/secret.key" ]; then
    echo "[entrypoint] 使用已有签名密钥 $DATA_DIR/secret.key"
fi

exec "$@"
