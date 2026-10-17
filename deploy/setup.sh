#!/usr/bin/env bash
# ============================================================================
# SayIt 服务器初始化（Ubuntu 22.04 / 24.04，在服务器上以 root 运行）
#
#   bash setup.sh sayit.example.com          # 只装到"能跑"，证书自己配
#   bash setup.sh sayit.example.com certbot  # 顺便用 Let's Encrypt 签好证书
#
# 前提：代码已经上传到 /opt/sayit（本脚本会用它里面的 requirements.txt）
# ============================================================================
set -euo pipefail

DOMAIN="${1:-}"
CERT_MODE="${2:-none}"          # none | certbot
APP_DIR=/opt/sayit
APP_USER=sayit
PORT=8787

if [[ -z "$DOMAIN" ]]; then
  echo "用法: bash setup.sh <域名> [certbot]"
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "请用 root 或 sudo 运行"
  exit 1
fi
if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
  echo "[!] $APP_DIR 里没有代码。先把项目传上去（见 DEPLOY.md 第 3 步）"
  exit 1
fi

echo "== 1/7 安装系统包 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx sqlite3 curl ca-certificates

PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "   python $PYVER"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "[!] Python 需要 3.10+，换 Ubuntu 22.04 / 24.04 镜像"; exit 1; }

echo "== 2/7 建系统用户 =="
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR/data" "$APP_DIR/certs"

echo "== 3/7 Python 虚拟环境 + 依赖 =="
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
# 国内机器用清华源快很多；香港/海外也够用
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" \
  -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "== 4/7 目录权限 =="
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env" 2>/dev/null || true

echo "== 5/7 systemd 服务 =="
install -m 644 "$APP_DIR/deploy/sayit.service" /etc/systemd/system/sayit.service
systemctl daemon-reload
systemctl enable --now sayit
sleep 2
systemctl --no-pager --lines=8 status sayit || true
curl -sf "http://127.0.0.1:$PORT/api/bootstrap" >/dev/null \
  && echo "   本机接口 OK" \
  || { echo "[!] 应用没起来，看 journalctl -u sayit -n 50"; exit 1; }

echo "== 6/7 nginx =="
mkdir -p /var/www/certbot
# 先只开 80：证书还没签的时候 443 会起不来
cat > /etc/nginx/sites-available/sayit <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 3600s;
        proxy_buffering off;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/sayit /etc/nginx/sites-enabled/sayit
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "== 7/7 证书 =="
if [[ "$CERT_MODE" == "certbot" ]]; then
  apt-get install -y -qq certbot
  certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
    --non-interactive --agree-tos -m "admin@$DOMAIN" || {
      echo "[!] 签发失败：确认 $DOMAIN 的 A 记录已经指向本机公网 IP，且安全组放行 80"; exit 1; }
  sed "s/sayit\.example\.com/$DOMAIN/g" "$APP_DIR/deploy/nginx-sayit.conf" \
    > /etc/nginx/sites-available/sayit
  nginx -t && systemctl reload nginx
  systemctl enable --now certbot.timer 2>/dev/null || true
  echo "   已启用 443，证书自动续期（systemctl list-timers | grep certbot）"
else
  echo "   跳过。下一步按 DEPLOY.md 第 6 步做：腾讯云免费证书 or certbot，"
  echo "   然后把 deploy/nginx-sayit.conf 里的域名和证书路径改好，覆盖"
  echo "   /etc/nginx/sites-available/sayit 再 nginx -t && systemctl reload nginx"
fi

echo
echo "==== 完成 ===="
echo "  应用： systemctl status sayit / journalctl -u sayit -f"
echo "  日志： /opt/sayit/data"
echo "  配置： /opt/sayit/data/settings.json（网页 /settings 改）"
