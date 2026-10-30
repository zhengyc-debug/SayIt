# 部署到腾讯云（手机免装证书的最终解法）

> 面向：想在任何网络下用手机练口语、又不想在手机上装证书。
> 目标形态：`https://sayit.你的域名.com` —— 真证书、走到哪都能用、断网也能在局域网用（走内网解析）。

---

## 0. 先把结论说清楚

**能解决，而且是最彻底的方式。** 但要分清"服务器解决了什么"：

| 问题 | 云服务器能不能解决 |
|---|---|
| 手机必须装证书 | ✅ 能。有域名 + 公网 IP 就能拿**真证书**（腾讯云免费 DV / Let's Encrypt），手机原生信任，什么都不用装 |
| 只能在同一 WiFi 下用 | ✅ 能。走 4G/5G 都能连 |
| 电脑必须开着 | ✅ 能。服务器 24 小时在线 |
| 延迟 | ❌ 反而会变高。音频要多绕一跳：手机 →（公网）→ 服务器 →（公网）→ 豆包 |
| 公网暴露风险 | ⚠️ 应用**没有登录**，谁拿到地址都能用你的豆包额度 —— 见第 9 步加密码 |

延迟大概是什么量级（单程）：手机 → 香港服务器 40~70ms，香港 → 豆包（国内）40~80ms。
一通对话累计多出 100ms 左右，**日常练口语能接受**，但不如局域网那种"零延迟"的跟手感。
如果你对延迟敏感，看第 1 节的"国内节点"方案（代价是要备案）。

架构变成：

```
手机 (Safari/Chrome)
   │  HTTPS + WSS  (真证书，443)
   ▼
腾讯云 nginx (TLS 终结, 反代, 加密码)
   │  HTTP / WS  (只监听 127.0.0.1:8787)
   ▼
uvicorn app.main:app  ──WSS──▶  豆包实时语音 API
                      └─HTTPS─▶  DeepSeek（复盘）
```

---

## 1. 选什么服务器

**产品**：腾讯云 **轻量应用服务器 Lighthouse**（不是 CVM）。
理由：带宽/流量打包、面板简单、自带防火墙、便宜；这个项目只是"转发 + 记账"，很轻。

**配置**：**2 核 2G / 40~50G SSD**，够用到你天天练。

**地域（最关键的一条）**：

| 地域 | 备案 | 延迟 | 适合 |
|---|---|---|---|
| **香港 / 新加坡** | **不需要** | 多 100ms 左右 | **推荐先选这个**：当天就能上线 |
| 上海 / 广州 / 北京 | **必须 ICP 备案**（7~20 天，服务器有效期 ≥3 个月） | 最低 | 对延迟极其敏感、且愿意等 |

> 国内节点不备案会怎样：域名解析到国内 IP 后，80/443 直接被拦，你签证书和访问都会失败。
> 备案期间网站也不能对外提供服务。所以除非你确定要长期用国内节点，否则先买香港。

**带宽/流量**：一通电话大概 **上行 256kbps + 下行 384kbps ≈ 0.3GB/小时**。
- 香港：30Mbps 峰值 + 月流量包（几百 GB～1TB），绰绰有余。
- 国内：3~6Mbps 固定带宽 + 流量包，一个人用也够（撑 5 路并发都没问题）。

**镜像**：**Ubuntu 24.04 LTS**（Python 3.12，直接能用；22.04 也行，是 3.10）。

**价格**：轻量 2C2G 大概 ¥60~100/月（新用户活动经常 ¥99/年）；域名 ¥30~100/年。以官网为准。

---

## 2. 买完之后马上做的 4 件事

1. **安全组/防火墙**（轻量控制台 → 防火墙）：放行 **22、80、443**。
   8787 不用对外开放 —— 应用只监听 127.0.0.1。
2. **登录**：
   ```bash
   ssh root@你的公网IP
   ```
   建议改用密钥登录（控制台可以一键绑定），密码登录容易被爆破。
3. **时区**（影响通话记录的时间显示）：
   ```bash
   timedatectl set-timezone Asia/Shanghai
   ```
4. **系统更新**：
   ```bash
   apt update && apt -y upgrade
   ```

---

## 3. 把代码传上去

在你 Windows 的 git-bash 里（不要传 `.venv`、`tmp`、`log`、`data/users.csv`）：

```bash
cd /e/SayIt

# 打包（排除本机虚拟环境、缓存、日志、以及本机的账号和配置）
tar --exclude='./.venv' --exclude='./tmp' --exclude='./log' \
    --exclude='__pycache__' --exclude='./data/recordings' \
    --exclude='./data/users.csv' --exclude='./data/settings.json' \
    -czf /tmp/sayit.tgz .

scp /tmp/sayit.tgz root@你的公网IP:/tmp/

ssh root@你的公网IP "mkdir -p /opt/sayit && tar xzf /tmp/sayit.tgz -C /opt/sayit && ls /opt/sayit"
```

`.env` 会一起传上去（里面有 API Key）。如果不想传，就在服务器上按 `.env.example` 新建一份。

> 之后每次更新代码，重复上面这两条 scp/tar 命令 + 第 8 步的 restart 即可。

---

## 4. 一键初始化

代码到位后，在服务器上：

```bash
cd /opt/sayit
bash deploy/setup.sh 你的域名.com          # 先只装到"能跑 + nginx 80"
```

它做了：装 python3/venv/nginx/sqlite → 建 `sayit` 系统用户 → 建 venv 装依赖 →
装 systemd 服务并启动 → 本机 curl 自检 → 写一个只开 80 的 nginx 站点。

看到 `本机接口 OK` 就成功了。此时可以先用 **电脑** 验证：

```bash
curl -s http://127.0.0.1:8787/api/bootstrap | head -c 200
```

---

## 5. 域名 + 证书（这一步解决"手机不装证书"）

### 5.1 解析

腾讯云 → DNS 解析（域名在哪买的都行，用那家的 DNS）：
加一条 **A 记录**：`sayit` → 服务器公网 IP。

验证：`nslookup sayit.你的域名.com` 应返回你的公网 IP。

### 5.2 证书（两条路，选一条）

#### 路线 A：腾讯云免费 SSL 证书（省事，推荐第一次用）

1. 控制台 → **SSL 证书** → 申请免费证书（DV，1 年）
2. 域名验证方式选 **DNS**（加一条 TXT，不用开 80 端口，香港/国内都能过）
3. 签发后下载 **Nginx** 格式，得到 `xxx_bundle.pem`（或 `.crt`）和 `xxx.key`
4. 传到服务器：
   ```bash
   scp xxx_bundle.pem xxx.key root@公网IP:/etc/nginx/certs/
   ```
5. 编辑 `/etc/nginx/sites-available/sayit`，用仓库里 `deploy/nginx-sayit.conf` 的内容覆盖，
   把 `sayit.example.com` 换成你的域名，并**启用路线 A 的那两行 `ssl_certificate`**（把路线 B 的注释掉）：
   ```nginx
   ssl_certificate     /etc/nginx/certs/xxx_bundle.pem;
   ssl_certificate_key /etc/nginx/certs/xxx.key;
   ```
6. 生效：
   ```bash
   nginx -t && systemctl reload nginx
   ```

#### 路线 B：Let's Encrypt（自动续期，一条命令）

```bash
cd /opt/sayit
bash deploy/setup.sh 你的域名.com certbot
```

它会在 80 端口用 webroot 验证拿证书，然后自动把站点换成 443 配置、开启 `certbot.timer` 续期。

> 如果 80 端口不通（比如国内未备案被拦），路线 B 走 `--dns` 验证：
> ```bash
> apt install -y certbot
> certbot certonly --manual --preferred-challenges dns -d sayit.你的域名.com
> ```
> 按提示去 DNS 加一条 TXT 记录即可。

### 5.3 验证

浏览器打开 `https://sayit.你的域名.com` —— **地址栏没有警告、没有"继续前往"**，
就说明手机端彻底不用管证书了。

---

## 6. 手机上跑起来

1. 手机浏览器打开 `https://sayit.你的域名.com`
2. 点「开始通话」，允许麦克风 → 能听到开场白就通了
3. 浏览器菜单 → **添加到主屏幕**（全屏 PWA，像原生 App）

> 在家想用最低延迟：把域名的那条 A 记录改成电脑的内网 IP（如 192.168.3.8），
> 手机在同一个 WiFi 下解析到内网、直连电脑，证书照样有效（域名没变）。
> 出门再改回公网 IP —— 或者干脆两台都用，靠 DNS 切换。

---

## 7. 把本机数据搬过去（可选）

```bash
# Windows 上
scp /e/SayIt/data/sayit.db root@公网IP:/opt/sayit/data/
scp /e/SayIt/data/settings.json root@公网IP:/opt/sayit/data/

# 服务器上
chown sayit:sayit /opt/sayit/data/sayit.db /opt/sayit/data/settings.json
systemctl restart sayit
```

`sayit.db` 里是通话记录/复盘/错题本，`settings.json` 里是供应商配置和 API Key。

---

## 8. 日常运维

```bash
systemctl status sayit          # 是否在跑
journalctl -u sayit -f          # 实时日志（通话报错看这里）
systemctl restart sayit         # 改完代码 / 改完 .env 后
nginx -t                        # 改完 nginx 先测语法
```

**更新代码**：本地改完 → 重新 tar + scp → `systemctl restart sayit`（前端静态文件不用重启，刷新即可）。

**每日备份**（sqlite 热备）：
```bash
chmod +x /opt/sayit/deploy/backup.sh
echo '0 4 * * * bash /opt/sayit/deploy/backup.sh' | crontab -
```

**排错顺序**（按这个顺序查，99% 的问题在这几步）：
1. `systemctl status sayit` 挂了？→ `journalctl -u sayit -n 50`
2. 页面打不开 → `curl -I https://你的域名` / `nginx -t` / 安全组有没有开 443
3. 页面能开但点通话没反应 → F12 看 WS 是不是 403/超时；多半是 nginx 少了 `Upgrade` 头
4. WS 能连但通话 1 分钟后断 → `proxy_read_timeout` 没设
5. 听不到声音 → 服务器能不能出网：`curl -I https://openspeech.bytedance.com`
6. 复盘不出 → `/settings` 里看文本模型的 Key 和「启用复盘」

---

## 9. 登录（应用自带，不用 nginx auth_basic）

应用已经有登录系统：账号在 `data/sayit.db` 的 `accounts` 表里，由 admin 在
`/admin` 后台维护（新增人员 / 改密码 / 禁用 / 删除）。

部署后第一件事：在 `.env` 里定一个 admin 密码，再启动服务。

```bash
ssh root@你的公网IP
cd /opt/sayit
vi .env          # 加一行 SAYIT_ADMIN_PASSWORD=你的强密码
systemctl restart sayit
```

- 第一次启动时表是空的 → 用这个密码建 admin
- 已经有 admin → 不会覆盖（改了 `.env` 也没用）
- 忘了密码 → 再临时加 `SAYIT_ADMIN_PASSWORD_FORCE=1` 重启一次，然后删掉这行

不想进网页也能管账号：

```bash
cd /opt/sayit
sudo -u sayit ./.venv/bin/python -m app.store.accounts list
sudo -u sayit ./.venv/bin/python -m app.store.accounts add zhangsan 密码 --display 张三
sudo -u sayit ./.venv/bin/python -m app.store.accounts passwd zhangsan 新密码
```

老版本的 `data/users.csv`（username,password,display_name）会在首次启动时自动导入
（同名账号跳过 —— 不覆盖库里已有的密码），导完改名成 `users.csv.migrated` 并追加一行导入记录。
明文密码和老 pbkdf2 哈希都原样入库、都能登录，下次改密码时自动升级成新哈希。
不确定会导什么就先预览：

```bash
cd /opt/sayit
sudo -u sayit ./.venv/bin/python -m app.store.accounts import-csv --dry-run
sudo -u sayit ./.venv/bin/python -m app.store.accounts import-csv   # 真导
```

`.env` 里务必设一个固定的 `SECRET_KEY`（否则每次重启服务所有人要重新登录）：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
# 写进 /opt/sayit/.env 的 SECRET_KEY=
systemctl restart sayit
```

**不要再加 nginx `auth_basic`**：那样 WebSocket 拿不到登录态，而且手机上弹原生密码框体验很差。应用层的登录已覆盖所有页面、接口和 `/ws/call`。

> 想要更彻底的隔离：安全组里把 443 只放行你常用的 IP 段，或者用腾讯云「私有网络/白名单」。

---

## 10. 常见坑

- **国内节点 + 未备案域名**：80/443 被拦，证书签不下来，页面打不开。老老实实走香港，或者先备案。
- **安全组只开了 22**：最常见。80/443 都要开。
- **`Connection "upgrade"` 写死**：WebSocket 在非 upgrade 请求上会被转发成 `close`，用仓库里那份 `map $http_upgrade` 的配置。
- **忘了 `proxy_read_timeout`**：默认 60 秒，通话到 1 分钟必断，而且现象像"AI 突然不理我"。
- **`.env` 里 Key 有引号/空格**：systemd 的 `EnvironmentFile` 解析比 python-dotenv 严格，`KEY=xxx` 不要写成 `KEY = xxx` 或 `KEY="xxx"`。
- **服务器没配时区**：通话时间会差 8 小时（记录页看起来全乱）。
- **录音文件会越长越多**：`/opt/sayit/data/recordings`，定期删；备份脚本没包含它。
- **别把 `.venv` 一起传**：Windows 的 venv 到 Linux 不能用，服务器上重建（setup.sh 会做）。

---

## 附：文件清单

| 文件 | 用途 |
|---|---|
| `deploy/setup.sh` | 服务器一键初始化（装依赖 → 建用户 → venv → systemd → nginx 80） |
| `deploy/sayit.service` | systemd 单元（uvicorn，只监听 127.0.0.1:8787） |
| `deploy/nginx-sayit.conf` | nginx 反代 + TLS + WebSocket 长连接配置模板 |
| `deploy/backup.sh` | sqlite 热备份，配 cron 每天跑 |
