import logging
import os
import time

import psutil
from llama_cpp import Llama

from app import metrics
from app.config import settings

logger = logging.getLogger(__name__)


class ModelManager:
    def __init__(self):
        self.model: Llama | None = None
        self.model_loaded: bool = False

    def load_model(self) -> None:
        logger.info(f"Loading model from {settings.MODEL_PATH}")
        start = time.perf_counter()
        try:
            self.model = Llama(
                model_path=settings.MODEL_PATH,
                n_ctx=settings.MODEL_N_CTX,
                n_threads=settings.MODEL_N_THREADS,
                n_gpu_layers=0,
                use_mmap=True,
                use_mlock=False,
                verbose=False,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.model_loaded = True
            logger.info(f"Model loaded in {elapsed_ms:.0f}ms")
            metrics.set_model_loaded("smollm2-1.7b-q4", True)
            metrics.set_deployment_info(
                color=os.environ.get("DEPLOYMENT_COLOR", "unknown"),
                version="1.0.0",
            )
        except Exception:
            logger.exception("Failed to load model")
            raise

    def generate(
        self,
        prompt: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict:
        if not self.model_loaded or self.model is None:
            raise RuntimeError("Model is not loaded")

        max_tokens = max_tokens or settings.MODEL_MAX_TOKENS
        temperature = temperature if temperature is not None else settings.MODEL_TEMPERATURE

        start = time.perf_counter()
        response = self.model.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        text = response["choices"][0]["message"]["content"]
        tokens = response["usage"]["completion_tokens"]

        metrics.record_inference("smollm2-1.7b-q4", elapsed_ms / 1000, tokens)

        logger.info(
            "Inference complete",
            extra={"latency_ms": round(elapsed_ms, 1), "tokens_generated": tokens},
        )

        return {
            "text": text,
            "tokens_generated": tokens,
            "inference_ms": round(elapsed_ms, 1),
        }

    def health_check(self) -> dict:
        try:
            start = time.perf_counter()
            self.model.create_chat_completion(
                messages=[{"role": "user", "content": "Hi"}],
                max_tokens=5,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            return {
                "model_loaded": True,
                "inference_working": True,
                "test_inference_ms": round(elapsed_ms, 1),
            }
        except Exception as e:
            return {
                "model_loaded": self.model_loaded,
                "inference_working": False,
                "error": str(e),
            }

    def get_memory_usage(self) -> dict:
        proc = psutil.Process()
        mem = proc.memory_info()
        rss_mb = mem.rss / (1024 * 1024)
        vms_mb = mem.vms / (1024 * 1024)
        return {
            "rss_mb": round(rss_mb, 1),
            "vms_mb": round(vms_mb, 1),
            "percent": round(rss_mb / settings.CONTAINER_MEMORY_LIMIT_MB * 100, 1),
        }


model_manager = ModelManager()
