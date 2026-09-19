FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PORT=9020 \
    DATA_DIR=/data

# 国内构建可加：--build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX=
# 版本固定成实际验证过的版本；升级时改这里即可
ARG TELETHON_VERSION=1.45.0
ARG FLASK_VERSION=3.1.3
RUN if [ -n "$PIP_INDEX" ]; then \
      pip install --no-cache-dir "telethon==${TELETHON_VERSION}" "flask==${FLASK_VERSION}" -i "$PIP_INDEX"; \
    else \
      pip install --no-cache-dir "telethon==${TELETHON_VERSION}" "flask==${FLASK_VERSION}"; \
    fi

WORKDIR /app
COPY app /app

LABEL org.opencontainers.image.title="tg-forwarder" \
      org.opencontainers.image.description="Telegram 频道转发：按关键词正则过滤、事件驱动秒级转发、相册整组转发，带中文 Web 面板" \
      org.opencontainers.image.source="https://github.com/yq487900/tg-forwarder" \
      org.opencontainers.image.licenses="MIT"

VOLUME ["/data"]
EXPOSE 9020

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9020/login', timeout=5).status == 200 else 1)"

CMD ["python", "/app/main.py"]
