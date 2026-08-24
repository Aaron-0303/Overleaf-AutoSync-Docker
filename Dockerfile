ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG DEBIAN_MIRROR=https://mirrors.aliyun.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 默认使用国内镜像，加快中国大陆服务器构建速度。
# Git 用于每个论文项目的本地版本历史，不连接远程仓库。
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i \
        -e "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" \
        -e "s|https://deb.debian.org|${DEBIAN_MIRROR}|g" \
        -e "s|http://security.debian.org|${DEBIAN_MIRROR}|g" \
        -e "s|https://security.debian.org|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
      sed -i \
        -e "s|http://deb.debian.org|${DEBIAN_MIRROR}|g" \
        -e "s|https://deb.debian.org|${DEBIAN_MIRROR}|g" \
        -e "s|http://security.debian.org|${DEBIAN_MIRROR}|g" \
        -e "s|https://security.debian.org|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates git; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --index-url "${PIP_INDEX_URL}" -r requirements.txt
COPY app.py .
COPY config.example.yml ./config.example.yml

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

CMD ["python", "app.py"]
