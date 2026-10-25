# Docker 部署（nginx + sayit 两个容器）

镜像里只有代码和开场白音频，**不含任何 API Key、账号、通话记录**。
这些全部在运行时通过 `sayit-data` 卷挂进来。

---

## 1. 三个文件，三条命令

上线前只需要准备三样东西（都在仓库里，改一下即可）：

| 文件 | 要改什么 |
|---|---|
| `deploy/nginx/certs/` | 放 `keeplearning.icu_bundle.crt` + `keeplearning.icu.key` |
| `deploy/nginx/sayit.conf` | 域名换了就改 `server_name` |
| `docker-compose.yml` | 想固定 admin 密码就在 `environment` 里写 `SAYIT_ADMIN_PASSWORD` |

然后：

```bash
docker compose up -d --build

# 首次启动如果没设 SAYIT_ADMIN_PASSWORD，密码是随机生成的，从这里拿（只打印一次）
docker compose logs sayit | grep -A5 "已创建管理员账号"

# 起来之后确认两个容器都健康、nginx 能连上 sayit
docker compose ps
docker compose exec nginx wget -qO- http://sayit:8787/api/auth-state
```

打开 `https://keeplearning.icu` 用 admin 登录 → 设置页填 API Key →
「我的」页底部进 **后台 · 人员管理** 加人。

---

## 2. 网络拓扑

```
公网 80/443
   │
   ▼
nginx 容器（唯一发布端口的容器，80 + 443）
   │  sayit-net 内部 DNS：http://sayit:8787
   ▼
sayit 容器（不发布端口，只有 expose 8787）
   │
   ▼
豆包 wss（出网，不需要入向端口）
```

要点：

- **两个容器都在 `sayit-net` 里**，nginx 用服务名 `sayit` 访问应用 ——
  配置里那句 `proxy_pass http://sayit:8787` 就是为此。容器里的 `127.0.0.1`
  指的是 nginx 自己，照抄宿主机那份配置会 502。
- **sayit 不发布端口**（`ports` 只在 `debug` profile 里给本机调试用）。
  公网只能经 nginx 进来。
- **`depends_on: condition: service_healthy`** —— sayit 的 healthcheck 通过后
  才起 nginx，避免首次访问撞上 502。
- nginx 用的是 `deploy/nginx/sayit.conf`，不是宿主机那份。区别只有上游地址，
  外加一个 ACME 校验路径。
- `deploy/nginx/empty.conf` 覆盖掉 nginx 镜像自带的默认站点，
  否则它的 `listen 80` 会和我们的 `default_server` 抢请求。

本机调试时想直接打 8787：

```bash
docker compose --profile debug up -d      # 只额外发布 127.0.0.1:8787
```

---

## 3. 账号（SQLite，不再是 users.csv）

账号在 `sayit.db` 的 `accounts` 表里，服务启动时自动建表并保证有 admin。

| 场景 | 行为 |
|---|---|
| 首次启动 + 设了 `SAYIT_ADMIN_PASSWORD` | 用这个密码建 admin |
| 首次启动 + 没设 | 随机生成 14 位密码，打印一次到日志 |
| 已经有 admin | **什么都不做**，不覆盖已改过的密码 |
| 忘了密码 | `SAYIT_ADMIN_PASSWORD=新密码` + `SAYIT_ADMIN_PASSWORD_FORCE=1`，重启一次 |

`FORCE` 只在「环境变量里的密码和库里现有的不一样」时才重置一次，
所以把它留在 compose 里也不会每次重启都刷库（但改回 0 更清楚）。

后台（`/admin`，只有 admin 能进）：新增人员、改密码、禁用/启用、升降角色、删除。
**最后一个可用的 admin 不能被删除/降级/禁用**，防止把自己锁在外面。

命令行也留了入口（容器里同样可用）：

```bash
docker compose exec sayit python -m app.store.accounts list
docker compose exec sayit python -m app.store.accounts add zhangsan 密码 --display 张三
docker compose exec sayit python -m app.store.accounts passwd zhangsan 新密码
```

> 老版本留下的 `data/users.csv` 会在首次启动时**自动导入**（同名账号跳过），
> 然后改名成 `users.csv.migrated`。老手写的 pbkdf2 哈希和明文都能继续登录。

---

## 4. 数据与备份

所有状态都在一个卷里：`sayit-data` → 挂到 `/app/data`。

```
/app/data/
├─ sayit.db             账号（accounts）+ 通话记录 + 错题本 + 复盘（SQLite 单文件）
├─ secret.key           登录签名密钥
├─ users/<用户名>/settings.json   每人的配置和 API Key
├─ greetings/           开场白音频（镜像自带）
└─ recordings/          录音（目前未使用）
```

备份（热备，服务不用停）：

```bash
docker run --rm -v sayit-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/sayit-data-$(date +%F).tgz -C /data .
```

**迁移到新机器**：备份卷 → 新机器上 `docker volume create sayit-data` →
解包进去 → 起容器。

---

## 5. 证书

`deploy/nginx/certs/` 是只读挂载到容器 `/etc/nginx/certs` 的宿主机目录，
已经在 `.gitignore` 里（证书不进 Git）。

**腾讯云/阿里云免费证书**：下载「Nginx 格式」，把 `*_bundle.crt` 和 `*.key`
拷进 `deploy/nginx/certs/`，文件名与 `sayit.conf` 里的
`ssl_certificate` / `ssl_certificate_key` 对齐。

**Let's Encrypt**：`sayit.conf` 里 80 端口那段已经留了
`/.well-known/acme-challenge/` → `/var/www/certbot`，
compose 把 `./deploy/nginx/www` 挂在那儿，用 webroot 方式签/续即可。

换证书或改配置后热加载，不用重启容器：

```bash
docker compose exec nginx nginx -t && docker compose exec nginx nginx -s reload
```

> **证书必须和访问的域名匹配。** 用 IP 访问 + 域名证书，PC 上点"继续访问"能过，
> 但手机会直接拒绝 `wss` 握手，表现为"连接服务器失败"。

---

## 6. 常见问题

**nginx 起不来：`[emerg] cannot load certificate`**
`deploy/nginx/certs/` 里没有和配置同名的证书文件。先放证书再
`docker compose restart nginx`。

**页面 502 / bad gateway**
`docker compose ps` 看 sayit 是否 healthy；再看
`docker compose exec nginx wget -qO- http://sayit:8787/api/auth-state`。
不通就是两个容器不在同一网络，或 sayit 还没启动完。

**页面能开但点通话没反应**
F12 看 WS 是不是 403/超时 —— 多半是 nginx 少了 `Upgrade` 头，或者
`proxy_read_timeout` 没设（通话 1 分钟后断就是这个）。

**改完代码怎么生效**

```bash
docker compose up -d --build
```

**容器一直 unhealthy**

```bash
docker compose logs sayit --tail=50
```

常见原因：卷权限不对（宿主机目录挂载时要 `chown -R 10001:10001 ./data`）。

**要重置 admin 密码**
见上面第 3 节。注意：删了 `sayit.db` 等于把所有通话记录一起删掉，别这么干。

**想用宿主机目录而不是命名卷**

```yaml
volumes:
  - ./data:/app/data    # 记得 chown 10001:10001 ./data
```
