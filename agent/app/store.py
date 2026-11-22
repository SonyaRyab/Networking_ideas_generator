"""Фоновые задания, идемпотентность UUID и диагностическое состояние с TTL."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Settings
from .delivery import Delivery
from .ollama import AgentFailure, Ollama
from .schemas import Failure, GenerateRequest

logger = logging.getLogger("content_ideas_agent")
TERMINAL = {"completed", "failed"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list | None = None):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(message)


@dataclass
class Job:
    request: GenerateRequest
    status: str = "accepted"
    accepted_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    accepted_clock: float = field(default_factory=time.monotonic)
    terminal_clock: float | None = None
    total_segments: int = 0
    sent_segments: int = 0
    error: Failure | None = None
    failure_notified: bool | None = None

    def update(self, status: str):
        self.status, self.updated_at = status, now()
        if status in TERMINAL:
            self.terminal_clock = time.monotonic()

    def public_status(self) -> dict:
        body = {
            "messageId": self.request.messageId,
            "status": self.status,
            "acceptedAt": self.accepted_at,
            "updatedAt": self.updated_at,
            "totalSegments": self.total_segments,
            "sentSegments": self.sent_segments,
        }
        if self.error:
            body["error"] = self.error.model_dump()
            body["failureNotified"] = self.failure_notified
        return body


class JobStore:
    def __init__(self, settings: Settings, ollama: Ollama, delivery: Delivery):
        self.settings, self.ollama, self.delivery = settings, ollama, delivery
        self.jobs: dict[str, Job] = {}
        self.tasks: set[asyncio.Task] = set()
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_generations)

    def purge(self):
        cutoff = time.monotonic() - self.settings.terminal_ttl_seconds
        for key, job in list(self.jobs.items()):
            if job.terminal_clock is not None and job.terminal_clock <= cutoff:
                del self.jobs[key]

    def get(self, message_id: str) -> Job:
        self.purge()
        if message_id not in self.jobs:
            raise DomainError(404, "NOT_FOUND", "Задание не найдено или срок хранения истёк")
        return self.jobs[message_id]

    def accept(self, request: GenerateRequest) -> Job:
        # Метод не содержит await: проверка UUID и регистрация атомарны в одном event loop.
        self.purge()
        existing = self.jobs.get(request.messageId)
        if existing:
            if existing.request != request:
                raise DomainError(409, "CONFLICT", "UUID уже используется с другими параметрами")
            return existing
        if sum(job.status not in TERMINAL for job in self.jobs.values()) >= self.settings.max_pending_jobs:
            raise DomainError(503, "SERVICE_UNAVAILABLE", "Очередь агента заполнена, повторите запрос позже")
        job = Job(request)
        self.jobs[request.messageId] = job
        task = asyncio.create_task(self.run(job), name=f"generate:{request.messageId}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    async def generate(self, job: Job):
        async with self.semaphore:
            job.update("generating")
            return await self.ollama.generate(job.request)

    async def run(self, job: Job):
        try:
            # Дедлайн включает ожидание семафора и все корректирующие попытки модели.
            remaining = self.settings.generation_timeout_seconds - (time.monotonic() - job.accepted_clock)
            result = await asyncio.wait_for(self.generate(job), timeout=max(0, remaining))
            job.update("sending")

            def progress(sent: int, total: int):
                job.sent_segments, job.total_segments, job.updated_at = sent, total, now()

            await self.delivery.send(result, progress)
            job.update("completed")
        except asyncio.TimeoutError:
            await self.fail(
                job, Failure(code="GENERATION_TIMEOUT", message="Истекло время ожидания генерации")
            )
        except AgentFailure as exc:
            await self.fail(job, exc.failure)
        except asyncio.CancelledError:
            # При остановке процесса записи исчезнут; транспорт завершит запрос своим таймером.
            raise
        except Exception:
            logger.exception("Unexpected background failure messageId=%s", job.request.messageId)
            await self.fail(
                job, Failure(code="GENERATION_FAILED", message="Внутренняя ошибка обработки задания")
            )

    async def fail(self, job: Job, failure: Failure):
        job.error = failure
        job.failure_notified = False
        # Состояние терминальное после попытки уведомления, чтобы TTL не удалил активную запись.
        try:
            await self.delivery.notify_failure(job.request.messageId, failure)
            job.failure_notified = True
        except AgentFailure:
            logger.warning(
                "Failure notification unavailable messageId=%s code=%s", job.request.messageId, failure.code
            )
        finally:
            job.update("failed")

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
