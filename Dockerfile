# Anamnesis node: your always-on, self-hosted memory (sync hub + connector for web chats).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ANAMNESIS_DATA_DIR=/data \
    ANAMNESIS_MEMORY_SYNC=false \
    PORT=8080

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 10001 anamnesis \
    && mkdir -p /data && chown anamnesis /data

USER anamnesis
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["anam", "node", "serve"]
