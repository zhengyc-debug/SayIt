# syntax=docker/dockerfile:1
# SayIt 镜像
#
# 构建：
#   docker build -t sayit:latest .
#
# 运行（首次需要挂载 data 卷并准备 users.csv）：
#   docker run -d --name sayit -p 8787:8787 \
#     -v sayit-data:/app/data \
#     sayit:latest
#
# 多阶段构建：编译期依赖只留在 builder 阶段，
# 最终镜像里没有编译器，体积小、攻击面也小。

# ===========================================================================
# 阶段 1：装依赖到独立 venv
# ===========================================================================
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 编译某些包需要；只在 build 阶段存在
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 只复制 requirements.txt —— 改业务代码不会触发重装依赖（利用层缓存）
COPY requirements.txt .

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# ===========================================================================
# 阶段 2：运行时
# ===========================================================================
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED: 日志实时进 docker logs
# PYTHONDONTWRITEBYTECODE: 容器里不生成 .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

# curl 仅供 HEALTHCHECK；tini 回收僵尸进程并正确转发信号
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# 业务代码（.dockerignore 已排除 .env / data/ / .venv）
COPY app/   ./app/
COPY web/   ./web/
COPY tools/ ./tools/
COPY main.py requirements.txt ./

# ---- 用户与数据目录 ----
# 不用 root 跑服务；uid/gid 固定 10001，挂 volume 时便于对权限
RUN useradd --system --uid 10001 --shell /usr/sbin/nologin sayit \
 && mkdir -p /app/data/greetings /app/data/recordings /app/data/users /app/log \
 && chown -R sayit:sayit /app

# 开场白音频（AI 自己念的 wav）随镜像发布，新部署直接有开场语音。
# 这是生成产物，不含用户隐私。
COPY --chown=sayit:sayit data/greetings/ ./data/greetings/

# 账号模板（真正生效的是挂载进来的 data/users.csv）
COPY --chown=sayit:sayit data/users.example.csv ./data/

# 启动脚本：首次启动自动建账号，否则空卷起来没法登录
COPY --chown=sayit:sayit docker/entrypoint.sh /usr/local/bin/sayit-entrypoint.sh
RUN chmod +x /usr/local/bin/sayit-entrypoint.sh  && chmod -R a+rX /app/data/greetings

USER sayit

EXPOSE 8787

# 健康检查走免登录的 /api/auth-state（未登录也返回 200）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8787/api/auth-state >/dev/null || exit 1

# tini 当 PID 1：uvicorn 收到 SIGTERM 才能优雅关闭 WebSocket
# entrypoint.sh 负责数据目录与首个账号，然后 exec 到 CMD
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sayit-entrypoint.sh"]

# 必须绑 0.0.0.0 —— 容器内 127.0.0.1 无法被 -p 转发出去
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8787"]
