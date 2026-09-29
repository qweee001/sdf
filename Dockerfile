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
