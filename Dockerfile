# STAGE 1: Builder
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake gcc g++ && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --no-cache-dir \
    llama-cpp-python \
    fastapi \
    "uvicorn[standard]" \
    psutil \
    prometheus-client \
    pydantic-settings

# STAGE 2: Runtime
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

RUN useradd --create-home appuser

WORKDIR /app
COPY app/ ./app/

RUN mkdir -p /data /models && chown -R appuser:appuser /data /models

USER appuser

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=60s \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
