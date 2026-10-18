# Docker 部署

镜像里只有代码和开场白音频，**不含任何 API Key、账号、通话记录**。
这些全部在运行时通过 `data` 卷挂进来。

---

## 1. 最快路径（3 条命令）

```bash
cd /e/SayIt
docker compose up -d --build

# 首次启动会自动生成管理员密码，从这里拿
docker compose logs sayit | grep -A6 "已创建管理员账号"
```

日志里会打印：

```
==============================================================
  已创建管理员账号
    用户名: admin
    密码:   GJvWV8NfEGu4
  这个密码只显示这一次，请立即记录并登录后修改。
==============================================================
```

打开 `http://127.0.0.1:8787` 用这个密码登录，然后到 **设置** 里填自己的 API Key。

想自己指定密码，不要让它随机生成：

```bash
SAYIT_ADMIN_PASSWORD='你的强密码' docker compose up -d --build
```

---

## 2. 构建细节

两层构建：

| 阶段 | 作用 |
|---|---|
| `builder` | 装 `build-essential`，把依赖装进 `/opt/venv` |
| `runtime` | 只搬 venv 和代码，**没有编译器**，镜像更小 |

几个刻意的选择：

- **`COPY requirements.txt` 单独一层** —— 改业务代码不会触发重装依赖
- **非 root 运行**（uid 10001）—— 容器被突破也拿不到宿主机
- **`tini` 当 PID 1** —— uvicorn 才能收到 SIGTERM，WebSocket 优雅关闭
- **`--host 0.0.0.0`** —— 容器内监听 127.0.0.1 时 `-p` 转发不出去
- **HEALTHCHECK 打 `/api/auth-state`** —— 这个接口免登录，且总能返回 200

---

## 3. 数据与备份

所有状态都在一个卷里：`sayit-data` → 挂到 `/app/data`。

```
/app/data/
├─ sayit.db              所有用户的通话记录、错题本、复盘（SQLite 单文件）
├─ users.csv             账号表
├─ secret.key            登录签名密钥
├─ users/<用户名>/settings.json   每人的配置和 API Key
├─ greetings/            开场白音频（镜像自带）
└─ recordings/           录音（目前未使用）
```

备份（热备，服务不用停）：

```bash
docker exec sayit sh -c 'sqlite3 /app/data/sayit.db ".backup /tmp/bak.db"' 2>/dev/null \
  || docker cp sayit:/app/data/sayit.db ./sayit-$(date +%F).db
```

或者直接拷整个卷：

```bash
docker run --rm -v sayit-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/sayit-data-$(date +%F).tgz -C /data .
```

**迁移到新机器**：备份卷 → 新机器上先 `docker volume create sayit-data` → 解包进去 → 起容器。

---

## 4. 手机要用，必须上 HTTPS

浏览器规定：**非 HTTPS 页面不给麦克风**，WebSocket 也一样。容器本身只说 HTTP，
所以要在前面放一层 nginx 终结 TLS：

```nginx
server {
    listen 443 ssl;
    server_name sayit.你的域名.com;

    ssl_certificate     /etc/letsencrypt/live/sayit.你的域名.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sayit.你的域名.com/privkey.pem;

    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:8787;   # 指向 docker 映射出来的端口
        proxy_http_version 1.1;

        # WebSocket 必须透传这两个头
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 一通电话是十几分钟的 WS 长连接，默认 60s 会被掐断
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;

        # 音频是持续小包，缓冲只会增加延迟
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
```

> **证书必须和访问的域名匹配。** 用 IP 访问 + 域名证书，PC 上点"继续访问"能过，
> 但手机会直接拒绝 `wss` 握手，表现为"连接服务器失败"。

compose 里默认把端口绑在 `127.0.0.1`，就是给 nginx 反代用的。想直接局域网访问，
把 `ports` 改成 `"8787:8787"`。

---

## 5. 常见问题

**改完代码怎么生效**
```bash
docker compose up -d --build
```

**容器一直 unhealthy**
```bash
docker compose logs sayit --tail=50
```
常见原因：端口被占、卷权限不对（`chown -R 10001:10001` 宿主机目录）。

**要重置账号**
```bash
docker exec -it sayit sh -c 'rm /app/data/users.csv' && docker compose restart sayit
```
重启会重新生成一个随机密码。

**想用宿主机目录而不是命名卷**
```yaml
volumes:
  - ./data:/app/data    # 记得 chown 10001:10001 ./data
```

**镜像体积**
`python:3.12-slim` 打底，最终约 200 MB 左右（大头是 Python 和 pydantic-core 的 wheel）。
