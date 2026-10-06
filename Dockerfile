FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && \
    apt-get install --no-install-recommends --yes gosu && \
    rm -rf /var/lib/apt/lists/* && \
    addgroup --system app && \
    adduser --system --ingroup app app && \
    mkdir -p /data && \
    chown -R app:app /app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 發版閘門（獨立階段，不進最終映像）------------------------------------
# 測試不過就讓建置失敗，Railway 也就不會部署這個提交。外部審查曾抓到「同一提交
# 測試失敗卻仍部署成功」13 次，因為 Railway 只認 push、不看 CI。
# 三個踩過的坑寫在這裡，免得又被改回去：
#   1) .dockerignore 必須允許 tests（曾經排除，COPY 直接失敗）
#   2) 測試會 import app、讀 tools/ 的校準工具，也會讀 Dockerfile 本身
#   3) 已知抖動（影片渲染與語音派送的時序測試）先明確 deselect，其餘全跑
FROM python:3.12-slim AS gate
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest pytest-asyncio
COPY app ./app
COPY tests ./tests
COPY tools ./tools
COPY Dockerfile ./Dockerfile
# env 只留最小集合：容器裡的生產環境變數不該左右測試結果
RUN env -i PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/bin HOME=/tmp LANG=C.UTF-8 \
        python -m pytest -q \
        --deselect tests/test_live_test.py::test_video_render_runs_in_own_task_without_blocking_text_or_voice

# ---- 最終映像 -------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && \
    apt-get install --no-install-recommends --yes gosu && \
    rm -rf /var/lib/apt/lists/* && \
    addgroup --system app && \
    adduser --system --ingroup app app && \
    mkdir -p /data && \
    chown -R app:app /app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Windows 檢出時 core.autocrlf 會把這個腳本變成 CRLF，shebang 就成了
# "#!/bin/sh\r"，runc 找不到 /bin/sh\r，容器會一路重啟到健康檢查超時。
# .gitattributes 已經鎖住 LF，這裡再清一次，讓任何人用任何平台打包都不會炸。
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod 0755 /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
