# SayIt

> 一个能"打电话"的英语口语陪练 agent。点一下拨号键，就能和一个说地道美式英语的 AI 实时对话，可以随时打断它。

当前状态：**Stage 0 + Stage 1 已完成并实测通过**（真实语音、全双工、可打断、字幕、通话记录、开场白）。

---

## 1. 跑起来

```bash
cd E:\SayIt
run_https.bat        # 手机 + 电脑都要用（含局域网 HTTPS）
run.bat              # 只在本机用、且不想折腾证书
```

两个 .bat 都会：自动建项目 venv（首次）→ 装依赖 → 清 PYTHONPATH → 启动。

### 运行环境（重要）

项目用**自己的虚拟环境** `E:\SayIt\.venv`（Python 3.12.4），不用系统全局环境。

| | |
|---|---|
| 解释器 | `E:\SayIt\.venv\Scripts\python.exe` |
| 依赖位置 | `E:\SayIt\.venv\Lib\site-packages` |
| 手工启动 | `env -u PYTHONPATH .venv/Scripts/python.exe tools/serve.py` |

**为什么启动脚本里要 `set PYTHONPATH=`**
这台机器上 Hermes Agent 会把 `PYTHONPATH` 指向它自己的内置环境
（`...\hermes\installs\...\venv\Lib\site-packages`，而且是用 3.14 编译的）。
不清空的话，**任何** python ——包括你新建的 venv——都会先 import 到 Hermes 的包，
表现出来就是 `ModuleNotFoundError: pydantic_core._pydantic_core` 这种莫名其妙的错。
踩过一次，别再踩。

排查"到底哪个 python 在跑"：`curl -s http://127.0.0.1:8788/api/health`，
`runtime` 字段会直接告诉你可执行文件、版本、是不是项目自己的 venv：

```json
"runtime": {
  "executable": "E:\\SayIt\\.venv\\Scripts\\python.exe",
  "version": "3.12.4",
  "is_venv": true,
  "in_project_venv": true
}
```

打开 <http://127.0.0.1:8787> ，点绿色拨号键即可通话。

底部四个标签页（`web/nav.js` 统一注入，改一处全站同步）：

| 标签 | 路径 | 干什么 |
|---|---|---|
| 通话 | `/` | 选场景/语言/水平 → 拨号。场景和等级是 chip，不是下拉框 |
| 记录 | `/history` | 逐句 transcript + 当通复盘；「再练一次」把场景带回复用 |
| 错题本 | `/stats` | 累计数据 + 错误类型分布 + 你实际说错过的句子 |
| 我的 | `/me` | 用户画像（自动算的）+ 关于你自己的备注 |

设置页 `/settings` 从右上角齿轮进（换供应商 / 音色 / 复盘模型）——属于低频操作，不占标签位。

UI 约定：
* `<select>` 一律 `appearance:none`（Chrome 会用系统白底画原生下拉，深色主题下白字白底看不清）
* 矮屏（`max-height:720px`）自动缩小头像和字幕区，媒体查询要写在基础规则之后
* 页面底部留 `--nav-h` 的 padding，避免内容被固定标签栏盖住

> 想彻底不折腾手机证书？**部署到云服务器 + 域名 + 真证书**，见 [`DEPLOY.md`](DEPLOY.md)。
> 目标形态：`https://sayit.你的域名.com`，手机什么都不用装，4G 也能用。

### 手机访问（必须 HTTPS）

浏览器规定：页面不在 `localhost` 上时，必须 HTTPS 才允许用麦克风。所以手机不能开 `http://`。

```bash
run_https.bat
# 等价于 python tools/serve.py
```

它一次做完四件事：
1. 找出本机所有 IPv4，用 `cryptography` 签发**带正确 SAN** 的自签证书（IP 换了会自动重签）
2. 绑 `0.0.0.0:8787`（默认的 `127.0.0.1` 手机连不上）
3. 加防火墙放行规则 `SayIt 8787`（需管理员）
4. 打印手机上该用的地址

启动后控制台会直接告诉你 `手机：https://192.168.x.x:8787 <<< 用这个`。

#### ⚠ 手机必须先安装证书，否则一定连不上通话

自签证书在手机上会有一个很容易误判的坑：

- **页面能打开**（可以点"高级 → 继续前往"跳过警告）
- **但 WebSocket 连不上**（页面里显示"连接服务端失败"）

原因：跳过证书警告只对"页面导航"生效，`wss://` 握手没有任何可点击的"继续前往"，
浏览器直接拒绝为不受信任的证书建立 WebSocket。（本机用 `curl -k` / 桌面端看不出问题，
一上手机就废。）

**解决（1 分钟）**：在手机浏览器里直接打开

```
https://192.168.3.8:8787/ca.cer
```

下载后安装为受信任根证书：

* **Android**：设置 → 安全 → 加密与凭据 → 安装证书 → **CA 证书** → 选刚下载的文件
  （会提示"你的数据可能被监视"，这是用户装 CA 的标准提示，确认即可）
* **iOS**：用 Safari 打开上面的链接 → 允许下载描述文件 →
  设置 → 已下载描述文件 → 安装 → 最后必须再去
  **设置 → 通用 → 关于本机 → 证书信任设置 → 打开"SayIt Local"的完全信任**
  （iOS 少这一步就还是不通）

装完刷新页面，地址栏不再报警告，通话就能接通了。

> 嫌麻烦想跳过这一步？见下一节。

### 手机上不想装证书：三条路（都能免掉「安装 CA」）

先说清楚一条硬规则：**麦克风只在「安全上下文」里可用** —— `https://` 或 `localhost`。
`http://192.168.x.x` 永远拿不到麦克风，这是浏览器定的，不是本项目的限制。
所以「不装证书」本质上是「换一个浏览器已经信任的地址」，只有下面几条：

| 方案 | 手机要装什么 | 需要联网 | 延迟 | 适用 |
|---|---|---|---|---|
| **A. USB 反向转发**（`tools/usb-android.bat`） | 什么都不装 | 不用 | 最低 | **Android**，自己练最省事 |
| **B. 真域名 + 免费 DV 证书** | 什么都不装 | 不用（解析到内网 IP 即可） | 最低 | 长期用，iPhone/Android 都行，最推荐 |
| **C. 临时隧道**（`tools/tunnel.bat`） | 什么都不装 | 要 | 高 | 临时给 iPhone 试一下 |
| （基线）自签证书 + 装 CA | 装一次 CA | 不用 | 最低 | 不想买域名时 |

**A. USB 反向转发（Android，零成本）**

```bat
run.bat                  :: 终端 1：纯 HTTP，只绑 127.0.0.1
tools\usb-android.bat    :: 终端 2：adb reverse tcp:8787 tcp:8787
```
手机浏览器打开 `http://localhost:8787`。`localhost` 属于安全上下文，麦克风照常可用，
完全不涉及证书；手机得插着 USB（拔了重插，重跑一次脚本）。
需要 `adb`：`winget install --id Google.PlatformTools -e`。

**B. 真域名 + 免费 DV 证书（最推荐，iPhone 也行）**

1. 随便一个域名（几十块一年），加一条 A 记录：
   `sayit.你的域名.com` → 电脑的内网 IP（例如 192.168.3.8），**不需要公网 IP**。
2. 在云厂商申请免费 DV 证书（DNS 验证，只需证明域名归你），下载 nginx 格式。
3. 放进 `certs/`，启动时指定：

```bat
python tools\serve.py --cert certs\sayit.pem --key certs\sayit.key --domain sayit.你的域名.com
```

手机打开 `https://sayit.你的域名.com:8787` —— 真证书，直接受信任，不用装 CA、不用点「继续前往」，
而且完全走局域网，断网也能用。证书一年一续（或 acme.sh `--dns` 自动续）。

**C. 临时隧道（给 iPhone 临时试一下）**

```bat
run.bat
tools\tunnel.bat          :: 打印 https://xxx.trycloudflare.com
```
免注册、真证书、手机什么都不用装，4G 也能访问。但实时语音要绕到国外边缘节点，
延迟明显变高，长期练口语不建议。

进去后"添加到主屏幕"就是全屏 PWA。

---

## 2. 三个"注册表"决定了项目的可扩展性

这个项目 80% 的扩展需求，都是往下面三个地方加一条数据，**不用改逻辑代码**。

### 加一门语言 → `app/languages.py`

```python
"ja": {
    "code": "ja", "label": "Japanese", "label_zh": "日语",
    "instruction_word": "Japanese",          # 会拼进 system prompt："Speak ONLY Japanese"
    "native_name": "日本語",
    "levels": ["N5", "N4", "N3", "N2", "N1"],
    "default_level": "N4",
    "native_hint": "The learner's native language is Chinese.",
    "voices": {                               # 该供应商支持哪些音色
        "doubao": [{"id": "jp_male_xxx", "label": "XX · 男声"}],
        "qwen_omni": [], "openai_realtime": [],
    },
},
```

加完刷新页面，语言下拉框里就有了；不支持的供应商会自动置灰。

### 加一个场景 → `app/scenarios.py`

```python
"airport": {
    "id": "airport", "icon": "✈️", "label_zh": "机场值机", "label": "At the airport",
    "opening": {"en": "Good morning, may I see your passport?", "*": "Good morning!"},
    "prompt":  {"en": "You are check-in staff. ...", "*": "You are check-in staff."},
},
```

`opening` 是 AI 的第一句话，`prompt` 会拼进 system prompt。`"*"` 是语言兜底。

> 加新场景后要跑一次 `python tools/gen_greetings.py` 生成开场白音频（见第 4 节）。

### 加一个语音供应商 → `app/providers/`

1. 新建 `app/providers/xxx.py`，继承 `RealtimeProvider`，实现 5 个方法：
   `connect` / `send_audio` / `interrupt` / `close` / `recv`
2. 在 `app/providers/__init__.py` 的 `PROVIDERS` 里注册
3. 在 `app/config.py` 的 `DEFAULT_SETTINGS["providers"]` 加默认配置
4. 在 `app/languages.py` 对应语言的 `voices` 里填它的音色

业务代码（网关、前端）只认 `base.py` 里的 7 种事件，永远不 import 具体供应商。
现在已经有三个：`doubao`（已实测）、`qwen_omni`（骨架）、`openai_realtime`（代码已写未联调）。

**前端也能换**：设置页每个供应商一张卡，改 Key / 模型 / 音色 / 端点，点"测试连接"当场验证。
配置存在 `data/settings.json`，优先级 高于 `.env`。

---

## 3. 架构

```
浏览器 (PWA)
  ├─ AudioWorklet 采集 → PCM16 16k，每 20ms 一包（640 字节）
  ├─ 播放队列（AudioContext 调度）→ 收到 user_speech_started 立刻清空（打断）
  └─ WebSocket（文本控制 + 二进制音频）
        ↕
FastAPI 网关 (app/main.py)
  ├─ 鉴权代理（API Key 只在服务端，前端拿不到）
  ├─ 双向泵 + 字幕语义归一（累积式 / 增量式）
  ├─ 落库 SQLite（data/sayit.db）
  └─ 开场白下发
        ↕
豆包 端到端实时语音大模型 3.0（Seeduplex，全双工）
```

### WebSocket 协议（`/ws/call`）

| 方向 | 内容 |
|---|---|
| 上行 文本 | `{"type":"start", language, scenario, level, provider}` / `interrupt` / `mute` / `text` / `stop` |
| 上行 二进制 | PCM16 LE / 单声道 / 16000Hz / 640 字节 |
| 下行 | `ready`(含 greeting) / `audio`(base64 PCM 24k) / `caption` / `user_speech_started` / `turn_end` / `error` / `closed` |

---

## 4. 开场白是怎么来的（一个坑的绕法）

豆包 3.0 **没有让模型主动开口的上行事件**（`response.create`、`say_hello` 全部返回
`45000000 unknown event name`，实测枚举过 19 个候选名字）。模型只在你说话之后才回应。

所以接通瞬间要听到 AI 的声音，只能由前端播一段音频。为了让"第一句"和后面的对话
是同一个人，`tools/gen_greetings.py` 用**模型自己**把每个场景的开场白念一遍录下来：

```bash
python tools/gen_greetings.py          # 生成全部 语言×场景×音色
python tools/gen_greetings.py --force  # 强制重录（改了 prompt 之后）
```

产物在 `data/greetings/`，索引 `index.json`。前端在 `ready` 里拿到 `/greetings/xxx.wav` 直接播。
没有对应音频时不会硬播错音色，只显示文字。

---

## 5. 踩过的坑（都写进代码注释了）

| 坑 | 现象 | 结论 |
|---|---|---|
| `output.format.type` 传 `pcm` | 收到的是 **float32**，按 int16 播就是白噪声 | 必须传 **`pcm_s16le`**。这条文档里没写，实测出来的 |
| 服务未开通 | `403 / 45000030 requested resource not granted` | Key 有效但没授权该服务；控制台开通后**要等几分钟生效**（我第一次也是这原因） |
| 鉴权方式 | `45000010 Invalid X-Api-Key` | 新版控制台走 `X-Api-Key` 头；旧版要 `X-Api-Id` + `X-Access-Key` + `X-Api-Resource-Id` |
| 用户字幕重复 | 字幕出现 "hellohello this..." | 豆包 ASR 同时下发"小写流式假设"和"带标点精修版"，两条都是累积全文 → **取最长者，不拼接** |
| 无主动开口 | 接通后 AI 一声不吭 | 见第 4 节，用预录开场白 |
| 发送节奏 | 过快/过慢服务端报错 | 必须严格 20ms 一包；浏览器端在 worklet 里攒够 640 字节再发 |
| 关麦 | 静音后模型超时无响应 | 必须发 `input_audio_mute.commit` / `input_audio_unmute.commit` 保持流 |
| 断连 | `55000001 ContextCanceled` | 关闭前先发 `session.close` 等服务端确认，不能直接 disconnect |
| 外放 | AI 自己打断自己 | 用耳机；已开 `echoCancellation` 但外放仍有风险 |
| 前端改完不生效 | 以为代码错了 | 静态资源加了 `no-store` 中间件；另有 `S.wrap` vs `el.wrap` 的错误值得记一辈子 |

---

## 6. 目录

```
SayIt/
├─ app/
│  ├─ main.py              FastAPI：配置 API + WebSocket 网关（不含任何供应商代码）
│  ├─ config.py            配置中心（data/settings.json 优先，.env 兜底）
│  ├─ languages.py         ★ 语言注册表
│  ├─ scenarios.py         ★ 场景注册表
│  ├─ prompts.py           system prompt 组装（教学法规则在这里改）
│  ├─ db.py                SQLite：sessions + turns
│  └─ providers/
│     ├─ base.py           ★ 供应商抽象层（7 种事件 + 5 个方法）
│     ├─ doubao.py         豆包 3.0 全双工（已实测）
│     ├─ qwen_omni.py      阿里骨架（待实现）
│     └─ openai_realtime.py OpenAI（代码已写，未联调）
├─ web/                    无构建步骤的原生前端
│  ├─ index.html app.js audio-worklet.js   通话页
│  ├─ settings.html settings.js            设置页
│  └─ history.html                         通话记录
├─ tools/
│  ├─ stage0_test.py       命令行验证：送一段 wav，收回 AI 语音，打印延迟
│  ├─ gen_greetings.py     预生成开场白
│  └─ make_test_audio.ps1  生成测试音频
├─ data/                   settings.json / sayit.db / greetings/（都在 .gitignore 里）
└─ run.bat / run_https.bat
```

---

## 7. 验证命令

```bash
# 单条链路（不经过浏览器）
python tools/stage0_test.py

# 实测指标（2026-10 本机）
#   连接耗时        59 ms
#   用户说完 → AI 首帧音频   约 550 ms
```

---

## 8. Stage 2：从"能聊天"到"能练口语"（已完成）

### 8.1 挂断即复盘

挂断后自动弹报告，15 秒左右出结果：

- **总评** + **CEFR 估计** + 语法/流利/词汇三个分数
- **3~6 条可改进处**：`你原话 → 地道说法 → 中文一句话原因`，按 语法/时态/用词/语序 分类
- **顺手记住**：AI 用过、你可能不会的表达
- **下一通的重点**：一句话，会写进下一通的 prompt

对话中**不纠错**（只做 recast），错都留到复盘说 —— 一开口就被打断，口语是练不出来的。

### 8.2 错题本（`/stats`）

每条错误都落库，按类型聚合。**下次通话时 AI 会主动创造 2~3 个场景，逼你把错的句型再用对一次** ——
这是从 errors 表聚合出 `top_weakness` 后注入 prompt 实现的，不是噱头。

### 8.3 用户画像 / 长期记忆 —— 就用 SQLite，不引外部组件

设计上故意**不存一份会过期的静态档案**，而是每次通话前现场聚合最近 8 通：

| 字段 | 来源 | 表 |
|---|---|---|
| 实测水平 CEFR | 最近几份报告 level 的众数 | `reports` |
| 常犯错误类型 | 最近 N 通的错误按 type 计数 | `errors` |
| 最近聊过的话题 | 复盘时顺手产出的 `topics` | `reports` |
| 你自己写的备注 | 手填（"我下周有面试"这种模型推不出来的） | `profile` |

合成一段 ~300 字注进 system prompt，实测见 `注入画像 283 字` 日志。
**代价是一次 SQLite 查询、0 token**，且永远是最新的，没有同步问题。

### 8.4 需要配一个文本模型

复盘要调 LLM，任何 OpenAI 兼容端点都行。`/settings` → **复盘用文本模型** 填 Key 即可，
默认指向 `https://api.deepseek.com/v1` + `deepseek-chat`。

没配也不影响通话、字幕、错题记录，只是不出复盘。

### 8.5 数据表

```sql
sessions(id, started_at, ended_at, provider, language, scenario, level, turns, duration_ms, meta)
turns(id, session_id, role, text, ts)
reports(session_id, created_at, model, payload)   -- payload 是整份 JSON
errors(id, session_id, you_said, better, why, type, ts)
profile(id=1, display_name, notes, updated_at)    -- 唯一手写字段
```

全部在 `data/sayit.db`，`.gitignore` 已排除。

## 9. 还没做的

- [ ] 发音评测：讯飞 ISE 对重点句打分（自由对话打分需要 reference 文本，用 ASR 结果近似）
- [ ] 错题本 SRS 复习（按遗忘曲线把老错误重新排进对话）
- [ ] 通话录音回放 + 逐句跟读
- [ ] 更多语言（注册表已就位，缺音色和 prompt 校验）
- [ ] 口语流利度的客观指标（语速 wpm、停顿次数），从音频算，不靠模型猜
