import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import settings
from app.database import get_conversation_history, init_db, save_conversation
from app.health import record_request, router as health_router
from app.logging_config import setup_logging
from app.metrics import MetricsMiddleware, metrics_response, set_model_loaded, set_deployment_info
from app.model_manager import model_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting server")
    model_manager.load_model()
    init_db()
    logger.info(f"Server ready on port {settings.PORT}")
    yield
    logger.info("Server shutting down gracefully")


app = FastAPI(title="SmolLM2 Zero-Downtime Server", version="1.0.0", lifespan=lifespan)
app.add_middleware(MetricsMiddleware)
app.include_router(health_router)


class ChatRequest(BaseModel):
    message: str
    user_id: str = "anonymous"
    max_tokens: int | None = None
    temperature: float | None = None


class ChatWithContextRequest(BaseModel):
    message: str
    max_tokens: int | None = None
    temperature: float | None = None
    context: bool = False


@app.post("/chat")
async def chat(req: ChatRequest):
    request_id = str(uuid.uuid4())
    logger.info("Chat request received", extra={"request_id": request_id, "user_id": req.user_id})

    try:
        result = model_manager.generate(req.message, req.max_tokens, req.temperature)
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e), "request_id": request_id})

    save_conversation(req.user_id, req.message, result["text"], result["tokens_generated"], result["inference_ms"])
    record_request(result["inference_ms"])

    logger.info(
        "Chat request complete",
        extra={"request_id": request_id, "latency_ms": result["inference_ms"], "tokens_generated": result["tokens_generated"]},
    )

    return {
        "response": result["text"],
        "request_id": request_id,
        "tokens_generated": result["tokens_generated"],
        "inference_ms": result["inference_ms"],
        "model_version": "smollm2-1.7b-q4",
    }


@app.post("/chat/{user_id}")
async def chat_with_user(user_id: str, req: ChatWithContextRequest):
    request_id = str(uuid.uuid4())
    logger.info("Chat request received", extra={"request_id": request_id, "user_id": user_id})

    prompt = req.message
    if req.context:
        history = get_conversation_history(user_id, limit=5)
        if history:
            context_lines = []
            for h in reversed(history):
                context_lines.append(f"User: {h['message']}")
                context_lines.append(f"Assistant: {h['response']}")
            context_lines.append(f"User: {req.message}")
            prompt = "\n".join(context_lines)

    try:
        result = model_manager.generate(prompt, req.max_tokens, req.temperature)
    except RuntimeError as e:
        return JSONResponse(status_code=503, content={"error": str(e), "request_id": request_id})

    save_conversation(user_id, req.message, result["text"], result["tokens_generated"], result["inference_ms"])
    record_request(result["inference_ms"])

    return {
        "response": result["text"],
        "request_id": request_id,
        "tokens_generated": result["tokens_generated"],
        "inference_ms": result["inference_ms"],
        "model_version": "smollm2-1.7b-q4",
    }


@app.get("/metrics")
async def metrics():
    return metrics_response()
