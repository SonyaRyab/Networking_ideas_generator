"""Конфигурация для нативной Ollama и Docker на macOS/Windows/Linux."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b-instruct-2507-q4_K_M"
    transport_base_url: str = "http://127.0.0.1:8080"
    generation_timeout_seconds: float = 110
    model_attempts: int = 2
    num_ctx: int = 4096
    num_predict: int = 2400
    keep_alive: str = "5m"
    temperature: float = 0.6
    max_concurrent_generations: int = 1
    max_pending_jobs: int = 4
    segment_delay_ms: int = 150
    delivery_timeout_seconds: float = 3
    delivery_attempts: int = 2
    terminal_ttl_seconds: float = 900

    def __post_init__(self):
        for name in ("ollama_base_url", "transport_base_url"):
            url = urlsplit(getattr(self, name))
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                raise ValueError(f"{name}: требуется HTTP(S) URL без логина и пароля")
            if url.query or url.fragment:
                raise ValueError(f"{name}: query и fragment не допускаются")
        if not self.ollama_model.strip():
            raise ValueError("OLLAMA_MODEL не должен быть пустым")
        if not 100 <= self.segment_delay_ms <= 300:
            raise ValueError("SEGMENT_DELAY_MS должен быть от 100 до 300")
        if not 1 <= self.model_attempts <= 3 or not 1 <= self.delivery_attempts <= 3:
            raise ValueError("Количество попыток должно быть от 1 до 3")
        if min(self.num_ctx, self.num_predict, self.max_concurrent_generations, self.max_pending_jobs) < 1:
            raise ValueError("Лимиты должны быть положительными")
        if (
            min(self.generation_timeout_seconds, self.delivery_timeout_seconds, self.terminal_ttl_seconds)
            <= 0
        ):
            raise ValueError("Тайм-ауты должны быть положительными")
        if self.max_pending_jobs < self.max_concurrent_generations:
            raise ValueError("MAX_PENDING_JOBS должен быть не меньше MAX_CONCURRENT_GENERATIONS")
        if not 0 <= self.temperature <= 2:
            raise ValueError("OLLAMA_TEMPERATURE должен быть от 0 до 2")

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        strings = {
            "ollama_base_url": "OLLAMA_BASE_URL",
            "ollama_model": "OLLAMA_MODEL",
            "transport_base_url": "TRANSPORT_BASE_URL",
            "keep_alive": "OLLAMA_KEEP_ALIVE",
        }
        integers = {
            "model_attempts": "MODEL_ATTEMPTS",
            "num_ctx": "OLLAMA_NUM_CTX",
            "num_predict": "OLLAMA_NUM_PREDICT",
            "max_concurrent_generations": "MAX_CONCURRENT_GENERATIONS",
            "max_pending_jobs": "MAX_PENDING_JOBS",
            "segment_delay_ms": "SEGMENT_DELAY_MS",
            "delivery_attempts": "DELIVERY_ATTEMPTS",
        }
        floats = {
            "generation_timeout_seconds": "GENERATION_TIMEOUT_SECONDS",
            "temperature": "OLLAMA_TEMPERATURE",
            "delivery_timeout_seconds": "DELIVERY_TIMEOUT_SECONDS",
            "terminal_ttl_seconds": "TERMINAL_TTL_SECONDS",
        }
        values = {}
        for fields, cast in ((strings, str), (integers, int), (floats, float)):
            for field, env in fields.items():
                values[field] = cast(os.getenv(env, str(getattr(cls, field))))
        values["ollama_base_url"] = values["ollama_base_url"].rstrip("/")
        values["transport_base_url"] = values["transport_base_url"].rstrip("/")
        return cls(**values)
