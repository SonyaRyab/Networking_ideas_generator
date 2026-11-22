"""Параметры транспорта для локальной сети или ZeroTier."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    agent_base_url: str = "http://127.0.0.1:8000"
    cors_origins: str = "*"
    kafka_bootstrap_servers: str = "127.0.0.1:9092"
    kafka_topic: str = "content-idea-segments"
    kafka_group_id: str = "content-ideas-transport"
    loss_percent: int = 10
    assembly_interval_seconds: int = 3
    max_idle_cycles: int = 3
    generation_timeout_seconds: int = 120
    terminal_ttl_seconds: int = 900

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        values = cls(
            agent_base_url=os.getenv("AGENT_BASE_URL", cls.agent_base_url).rstrip("/"),
            cors_origins=os.getenv("CORS_ORIGINS", "*"),
            kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", cls.kafka_bootstrap_servers),
            kafka_topic=os.getenv("KAFKA_TOPIC", cls.kafka_topic),
            kafka_group_id=os.getenv("KAFKA_GROUP_ID", cls.kafka_group_id),
            loss_percent=int(os.getenv("LOSS_PERCENT", "10")),
            assembly_interval_seconds=int(os.getenv("ASSEMBLY_INTERVAL_SECONDS", "3")),
            max_idle_cycles=int(os.getenv("MAX_IDLE_CYCLES", "3")),
            generation_timeout_seconds=int(os.getenv("GENERATION_TIMEOUT_SECONDS", "120")),
            terminal_ttl_seconds=int(os.getenv("TERMINAL_TTL_SECONDS", "900")),
        )
        if not values.agent_base_url.startswith(("http://", "https://")):
            raise ValueError("AGENT_BASE_URL должен быть HTTP-адресом")
        if not 0 <= values.loss_percent <= 100:
            raise ValueError("LOSS_PERCENT должен быть от 0 до 100")
        if min(
            values.assembly_interval_seconds,
            values.max_idle_cycles,
            values.generation_timeout_seconds,
            values.terminal_ttl_seconds,
        ) <= 0:
            raise ValueError("Временные параметры должны быть положительными")
        return values
