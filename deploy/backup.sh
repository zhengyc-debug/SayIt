#!/usr/bin/env bash
# 每日备份 SayIt 的数据库（账号 / 每人配置 / 通话记录 / 签名密钥全在 sayit.db 里）
# crontab -e 加一行：
#   0 4 * * * bash /opt/sayit/deploy/backup.sh
set -euo pipefail

SRC=/opt/sayit/data
DEST=/opt/sayit-backup
KEEP_DAYS=30

mkdir -p "$DEST"
STAMP=$(date +%F_%H%M)

# sqlite 的 .backup 是热备，不怕服务正在写
sqlite3 "$SRC/sayit.db" ".backup '$DEST/sayit-$STAMP.db'"

find "$DEST" -type f -mtime +$KEEP_DAYS -delete
echo "[backup] $STAMP ok -> $DEST"
