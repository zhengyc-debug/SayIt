# SayIt 镜像
#
# 非 root 运行（uid 10001），数据全部走 /app/data 卷：
#   docker volume create sayit-data
#   docker run -d -p 8787:8787 -e SAYIT_ADMIN_PASSWORD=xxx -v sayit-data:/app/data sayit:latest
#
# 账号不再来自 data/users.csv：第一次启动由 app/store/accounts.py 在库里建 admin，
# 密码取环境变量 SAYIT_ADMIN_PASSWORD（没设就随机生成并打印一次到日志）。
FROM python:3.13-slim

# curl 只给 HEALTHCHECK 用（compose 的 healthcheck 打的是 curl，镜像里原本没有）
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r "requirements.txt"

COPY app ./app/
COPY web ./web/
COPY tools ./tools/
COPY main.py ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 镜像里只带开场白音频，账号/配置/记录运行时由卷挂进来
COPY data/greetings ./data/greetings/
RUN mkdir -p /app/data/users /app/data/recordings /app/log \
 && useradd -u 10001 -m -s /usr/sbin/nologin sayit \
 && chown -R 10001:10001 /app/data /app/log

USER 10001

ENV SAYIT_DATA_DIR=/app/data \
    SAYIT_LOG_DIR=/app/log \
    PORT=8787

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:8787/api/auth-state" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "8787"]