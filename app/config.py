from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    MODEL_PATH: str = "/models/smollm2.gguf"
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    MODEL_N_CTX: int = 1024
    MODEL_N_THREADS: int = 4
    MODEL_MAX_TOKENS: int = 256
    MODEL_TEMPERATURE: float = 0.7
    DB_PATH: str = "/data/conversations.db"
    LOG_LEVEL: str = "INFO"
    CONTAINER_MEMORY_LIMIT_MB: int = 2500

    class Config:
        env_prefix = ""
        env_file = ".env"


settings = Settings()
