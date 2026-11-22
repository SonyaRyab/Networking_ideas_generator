"""Временное состояние задания, дедупликация, сборка и журнал SSE."""

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from .config import Settings
from .schemas import GenerateRequest, GenerationResult, Manifest, Segment


logger = logging.getLogger("content_ideas_transport")
TERMINAL = {"completed", "failed"}


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class Event:
    id: int
    name: str
    data: dict


@dataclass
class Job:
    request: GenerateRequest
    fingerprint: str
    created_at: float
    agent_ack: asyncio.Future
    status: str = "accepted"
    manifest: Manifest | None = None
    manifest_fingerprint: str | None = None
    last_progress_at: float | None = None
    attempts: dict[int, str] = field(default_factory=dict)
    segments: dict[int, str] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    result: dict | None = None
    error: dict | None = None
    finished_at: float | None = None
    subscribers: int = 0

    def public_status(self) -> dict:
        body = {
            "messageId": self.request.messageId,
            "status": self.status,
            "receivedSegments": len(self.segments),
            "totalSegments": self.manifest.totalSegments if self.manifest else 0,
        }
        if self.result is not None:
            body["result"] = self.result
        if self.error is not None:
            body["error"] = self.error
        return body


class JobStore:
    def __init__(self, settings: Settings, bus, *, random_source=None, clock=None):
        self.settings = settings
        self.bus = bus
        self.jobs: dict[str, Job] = {}
        self.lock = asyncio.Lock()
        self.random_source = random_source or random.random
        self.clock = clock or time.monotonic

    def _purge(self) -> None:
        now = self.clock()
        for message_id, job in list(self.jobs.items()):
            if job.finished_at is not None and now - job.finished_at >= self.settings.terminal_ttl_seconds:
                del self.jobs[message_id]

    async def reserve(self, request: GenerateRequest) -> tuple[Job, bool]:
        fingerprint = request.model_dump_json()
        async with self.lock:
            self._purge()
            existing = self.jobs.get(request.messageId)
            if existing:
                if existing.fingerprint != fingerprint:
                    raise DomainError(409, "CONFLICT", "Идентификатор занят другим запросом")
                return existing, False
            job = Job(request, fingerprint, self.clock(), asyncio.get_running_loop().create_future())
            self.jobs[request.messageId] = job
            return job, True

    async def agent_accepted(self, job: Job) -> None:
        async with self.lock:
            if job.status == "accepted":
                job.status = "generating"
            if not job.agent_ack.done():
                job.agent_ack.set_result(True)

    async def agent_rejected(self, job: Job) -> None:
        async with self.lock:
            if job.status not in TERMINAL:
                self._fail(job, "SERVICE_UNAVAILABLE", "Агентный сервис недоступен")
            if not job.agent_ack.done():
                job.agent_ack.set_result(False)
        await self._notify(job)

    def _event(self, job: Job, name: str, data: dict) -> None:
        job.events.append(Event(len(job.events) + 1, name, data))

    def _fail(self, job: Job, code: str, message: str) -> None:
        job.status = "failed"
        job.error = {"code": code, "message": message}
        job.result = None
        job.finished_at = self.clock()
        self._event(
            job,
            "generation_error",
            {
                "type": "generation_error",
                "messageId": job.request.messageId,
                "sentAt": job.request.sentAt,
                "isError": True,
                "error": job.error,
            },
        )

    async def _notify(self, job: Job) -> None:
        async with job.condition:
            job.condition.notify_all()

    async def get(self, message_id: str) -> Job:
        async with self.lock:
            self._purge()
            job = self.jobs.get(message_id)
            if job is None:
                raise DomainError(404, "NOT_FOUND", "Запрос не найден или истёк TTL")
            return job

    async def record_manifest(self, message_id: str, manifest: Manifest) -> int:
        async with self.lock:
            self._purge()
            job = self.jobs.get(message_id)
            if job is None:
                raise DomainError(404, "NOT_FOUND", "Запрос не найден")
            if job.status in TERMINAL:
                raise DomainError(409, "CONFLICT", "Задание уже завершено")
            if manifest.sentAt != job.request.sentAt:
                raise DomainError(409, "CONFLICT", "Время манифеста не совпадает с запросом")
            fingerprint = manifest.model_dump_json()
            if job.manifest is not None:
                if fingerprint != job.manifest_fingerprint:
                    raise DomainError(409, "CONFLICT", "Манифест противоречит предыдущему")
                return 200
            job.manifest = manifest
            job.manifest_fingerprint = fingerprint
            job.last_progress_at = self.clock()
            job.status = "assembling"
            logger.info("Манифест %s: %s сегментов", message_id, manifest.totalSegments)
            return 201

    async def record_segment(self, segment: Segment) -> None:
        async with self.lock:
            self._purge()
            job = self.jobs.get(segment.messageId)
            if job is None:
                raise DomainError(404, "NOT_FOUND", "Запрос не найден")
            if job.status in TERMINAL:
                raise DomainError(409, "CONFLICT", "Задание уже завершено")
            manifest = job.manifest
            if manifest is None:
                raise DomainError(409, "CONFLICT", "Сначала требуется манифест")
            if (
                segment.sentAt != manifest.sentAt
                or segment.totalSegments != manifest.totalSegments
                or segment.segmentNumber > manifest.totalSegments
            ):
                raise DomainError(409, "CONFLICT", "Метаданные сегмента не совпадают с манифестом")
            fingerprint = segment.model_dump_json()
            old = job.attempts.get(segment.segmentNumber)
            if old is not None:
                if old != fingerprint:
                    raise DomainError(409, "CONFLICT", "Повторный номер содержит другой сегмент")
                return
            if self.random_source() < self.settings.loss_percent / 100:
                job.attempts[segment.segmentNumber] = fingerprint
                logger.info("Учебная потеря сегмента %s/%s", segment.messageId, segment.segmentNumber)
                return
            try:
                await self.bus.publish(segment.messageId, fingerprint.encode("utf-8"))
            except Exception as exc:
                logger.exception("Kafka не приняла сегмент %s/%s", segment.messageId, segment.segmentNumber)
                raise DomainError(503, "SERVICE_UNAVAILABLE", "Kafka недоступна") from exc
            job.attempts[segment.segmentNumber] = fingerprint
            logger.info("Сегмент %s/%s помещён в Kafka", segment.messageId, segment.segmentNumber)

    async def consume_segment(self, raw: bytes) -> None:
        try:
            segment = Segment.model_validate_json(raw)
        except ValidationError:
            logger.warning("Kafka вернула некорректный сегмент")
            return
        async with self.lock:
            job = self.jobs.get(segment.messageId)
            if job is None or job.status in TERMINAL or job.manifest is None:
                return
            if segment.totalSegments != job.manifest.totalSegments or segment.sentAt != job.manifest.sentAt:
                return
            old = job.segments.get(segment.segmentNumber)
            if old is not None:
                if old != segment.payload:
                    self._fail(job, "INVALID_RESULT", "Противоречащие сегменты в очереди")
                    changed = True
                else:
                    changed = False
            else:
                job.segments[segment.segmentNumber] = segment.payload
                job.last_progress_at = self.clock()
                changed = False
        if changed:
            await self._notify(job)

    async def report_failure(self, message_id: str, code: str, message: str) -> None:
        async with self.lock:
            self._purge()
            job = self.jobs.get(message_id)
            if job is None:
                raise DomainError(404, "NOT_FOUND", "Запрос не найден")
            if job.status == "failed" and job.error == {"code": code, "message": message}:
                return
            if job.status in TERMINAL:
                raise DomainError(409, "CONFLICT", "Задание уже завершено")
            self._fail(job, code, message)
            if not job.agent_ack.done():
                job.agent_ack.set_result(True)
        await self._notify(job)

    def _assemble(self, job: Job) -> GenerationResult:
        manifest = job.manifest
        assert manifest is not None
        text = "".join(job.segments[number] for number in range(1, manifest.totalSegments + 1))
        data = text.encode("utf-8")
        if len(data) != manifest.totalBytes or hashlib.sha256(data).hexdigest() != manifest.sha256:
            raise ValueError("Длина или SHA-256 результата не совпадает")
        result = GenerationResult.model_validate(json.loads(text))
        request = job.request
        if (
            result.messageId != request.messageId
            or result.sentAt != request.sentAt
            or result.topic != request.topic
            or result.contentType != request.contentType
            or result.ideaCount != request.ideaCount
        ):
            raise ValueError("Метаданные результата не совпадают с запросом")
        return result

    async def tick(self) -> None:
        notifications: list[Job] = []
        async with self.lock:
            self._purge()
            now = self.clock()
            for job in self.jobs.values():
                if job.status in TERMINAL:
                    continue
                manifest = job.manifest
                if manifest is None:
                    if now - job.created_at >= self.settings.generation_timeout_seconds:
                        self._fail(job, "GENERATION_TIMEOUT", "Агент не прислал манифест вовремя")
                        notifications.append(job)
                    continue
                if len(job.segments) == manifest.totalSegments:
                    try:
                        result = self._assemble(job)
                    except (ValueError, ValidationError, UnicodeError, KeyError, json.JSONDecodeError):
                        self._fail(job, "INVALID_RESULT", "Повреждённый или некорректный результат")
                    else:
                        job.result = result.model_dump(mode="json")
                        for group in result.groups:
                            self._event(
                                job,
                                "idea_group",
                                {
                                    "type": "idea_group",
                                    "messageId": job.request.messageId,
                                    "sender": "content-ideas-agent",
                                    "sentAt": job.request.sentAt,
                                    "isError": False,
                                    "group": group.model_dump(mode="json"),
                                    "totalGroups": len(result.groups),
                                },
                            )
                        self._event(
                            job,
                            "generation_completed",
                            {
                                "type": "generation_completed",
                                "messageId": job.request.messageId,
                                "sentAt": job.request.sentAt,
                                "isError": False,
                                "ideaCount": result.ideaCount,
                            },
                        )
                        job.status = "completed"
                        job.finished_at = now
                    notifications.append(job)
                elif job.last_progress_at is not None and now - job.last_progress_at >= (
                    self.settings.assembly_interval_seconds * self.settings.max_idle_cycles
                ):
                    self._fail(job, "ASSEMBLY_TIMEOUT", "Не все сегменты получены")
                    notifications.append(job)
        for job in notifications:
            await self._notify(job)
