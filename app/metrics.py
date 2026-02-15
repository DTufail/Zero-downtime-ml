import os
import time

import psutil
from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ── Metric definitions ──

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

model_inference_duration_seconds = Histogram(
    "model_inference_duration_seconds",
    "Model inference time in seconds",
    ["model_name"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0],
)

active_requests = Gauge("active_requests", "Number of in-flight requests")

model_loaded = Gauge(
    "model_loaded",
    "Whether the model is loaded (1=loaded, 0=not loaded)",
    ["model_name"],
)

memory_usage_bytes = Gauge("memory_usage_bytes", "Process RSS memory in bytes")

tokens_generated_total = Counter(
    "tokens_generated_total",
    "Total tokens generated",
    ["model_name"],
)

deployment_color = Info("deployment", "Current deployment color and version")


# ── Path normalization ──

def normalize_path(path: str) -> str:
    """Normalize dynamic path segments to prevent metric label explosion."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "chat":
        return "/chat/{user_id}"
    return path


# ── Helper functions ──

def record_inference(model_name: str, duration_seconds: float, num_tokens: int):
    """Record an inference event in metrics."""
    model_inference_duration_seconds.labels(model_name=model_name).observe(duration_seconds)
    tokens_generated_total.labels(model_name=model_name).inc(num_tokens)


def update_memory_metric():
    """Update the memory usage gauge."""
    process = psutil.Process()
    memory_usage_bytes.set(process.memory_info().rss)


def set_model_loaded(model_name: str, loaded: bool):
    """Update the model loaded gauge."""
    model_loaded.labels(model_name=model_name).set(1 if loaded else 0)


def set_deployment_info(color: str, version: str):
    """Set deployment info metric."""
    deployment_color.info({"color": color, "version": version})


def metrics_response() -> Response:
    """Generate Prometheus metrics response."""
    update_memory_metric()
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ── Middleware ──

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)

        active_requests.inc()
        start = time.perf_counter()

        try:
            response = await call_next(request)
            duration = time.perf_counter() - start
            normalized = normalize_path(request.url.path)

            http_requests_total.labels(
                method=request.method,
                path=normalized,
                status_code=response.status_code,
            ).inc()

            http_request_duration_seconds.labels(
                method=request.method,
                path=normalized,
            ).observe(duration)

            return response
        except Exception:
            http_requests_total.labels(
                method=request.method,
                path=request.url.path,
                status_code=500,
            ).inc()
            raise
        finally:
            active_requests.dec()
