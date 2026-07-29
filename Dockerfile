FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEMORY_DB_PATH=/data/memory.db

WORKDIR /app

RUN addgroup --system app && \
    adduser --system --ingroup app app && \
    mkdir -p /data && \
    chown -R app:app /app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app ./app

USER app

VOLUME ["/data"]

CMD ["python", "-m", "app.main"]

