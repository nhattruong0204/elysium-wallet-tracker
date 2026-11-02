FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY agora_tracker ./agora_tracker
COPY alembic.ini ./

# Non-root; /data is pre-created so a named volume inherits this ownership.
RUN useradd --uid 10001 --create-home tracker \
    && mkdir -p /data /config \
    && chown tracker:tracker /data
USER tracker

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "agora_tracker", "--health"]

ENTRYPOINT ["python", "-m", "agora_tracker", "--config", "/config/config.yaml"]
CMD []
