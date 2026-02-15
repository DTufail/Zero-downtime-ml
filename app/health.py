import os
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.model_manager import model_manager

router = APIRouter()

_start_time = time.time()
_requests_served = 0
_total_latency_ms = 0.0


def record_request(latency_ms: float) -> None:
    global _requests_served, _total_latency_ms
    _requests_served += 1
    _total_latency_ms += latency_ms


@router.get("/healthz")
async def liveness():
    return {"status": "alive"}


@router.get("/ready")
async def readiness():
    health = model_manager.health_check()
    memory = model_manager.get_memory_usage()

    if (
        health.get("model_loaded")
        and health.get("inference_working")
        and memory["percent"] < 95
    ):
        return {
            "status": "ready",
            "inference_ms": health.get("test_inference_ms"),
            "memory_pct": memory["percent"],
        }

    reason = health.get("error", "unknown")
    if memory["percent"] >= 95:
        reason = f"memory at {memory['percent']}%"
    elif not health.get("model_loaded"):
        reason = "model not loaded"

    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "reason": reason},
    )


@router.get("/health/deep")
async def deep_health():
    health = model_manager.health_check()
    memory = model_manager.get_memory_usage()
    uptime = time.time() - _start_time
    avg_latency = (_total_latency_ms / _requests_served) if _requests_served > 0 else 0

    from app.metrics import active_requests as ar_gauge

    return {
        "status": "ok" if health.get("inference_working") else "degraded",
        "model_loaded": health.get("model_loaded", False),
        "model_name": "smollm2-1.7b-q4",
        "inference_test_ms": health.get("test_inference_ms"),
        "memory_used_mb": memory["rss_mb"],
        "memory_limit_mb": settings.CONTAINER_MEMORY_LIMIT_MB,
        "memory_pct": memory["percent"],
        "uptime_seconds": round(uptime, 1),
        "requests_served": _requests_served,
        "avg_latency_ms": round(avg_latency, 1),
        "active_requests": ar_gauge._value.get(),
        "version": "1.0.0",
        "container_id": os.environ.get("HOSTNAME", "unknown"),
        "color": os.environ.get("DEPLOYMENT_COLOR", "unknown"),
    }
