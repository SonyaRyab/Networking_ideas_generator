"""FastAPI транспортного уровня: HTTP, Kafka и Server-Sent Events."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError

from .config import Settings
from .kafka_bus import KafkaBus
from .schemas import Failure, GenerateRequest, Manifest, Segment, check_uuid4
from .store import DomainError, JobStore, TERMINAL


logger = logging.getLogger("content_ideas_transport")
MAX_BODY_BYTES = 64 * 1024


def error_response(status: int, code: str, message: str, details: list | None = None) -> JSONResponse:
    body = {"code": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


async def parse_body(request: Request, model):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise DomainError(415, "UNSUPPORTED_MEDIA_TYPE", "Требуется application/json")
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > MAX_BODY_BYTES:
        raise DomainError(413, "PAYLOAD_TOO_LARGE", "Тело запроса превышает 64 КиБ")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise DomainError(413, "PAYLOAD_TOO_LARGE", "Тело запроса превышает 64 КиБ")
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DomainError(400, "INVALID_JSON", "Некорректный JSON") from exc
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        details = [
            {"field": ".".join(map(str, error["loc"])), "message": error["msg"]}
            for error in exc.errors()
        ]
        raise BodyValidationError(details) from exc


class BodyValidationError(DomainError):
    def __init__(self, details: list):
        super().__init__(422, "VALIDATION_ERROR", "Ошибка полей запроса")
        self.details = details


def validate_id(message_id: str) -> str:
    try:
        return check_uuid4(message_id)
    except ValueError as exc:
        raise DomainError(422, "VALIDATION_ERROR", "Некорректный UUID") from exc


def create_app(
    settings: Settings | None = None,
    bus=None,
    agent_http: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings.from_environment()
    bus = bus or KafkaBus(settings)
    own_http = agent_http is None
    agent_http = agent_http or httpx.AsyncClient()
    store = JobStore(settings, bus)
    ticker: asyncio.Task | None = None

    async def tick_loop():
        while True:
            await asyncio.sleep(settings.assembly_interval_seconds)
            await store.tick()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ticker
        await bus.start(store.consume_segment)
        ticker = asyncio.create_task(tick_loop())
        yield
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        await bus.stop()
        if own_http:
            await agent_http.aclose()

    app = FastAPI(title="Content Ideas Transport", version="1.1.0", lifespan=lifespan)
    origins = [item.strip() for item in settings.cors_origins.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )
    app.state.store = store
    app.state.bus = bus

    def exact_openapi():
        if app.openapi_schema is None:
            source = Path(__file__).resolve().parents[1] / "transport.openapi.yaml"
            app.openapi_schema = yaml.safe_load(source.read_text(encoding="utf-8"))
        return app.openapi_schema

    app.openapi = exact_openapi

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, error: DomainError):
        details = error.details if isinstance(error, BodyValidationError) else None
        return error_response(error.status, error.code, error.message, details)

    @app.post("/api/v1/requests", status_code=202)
    async def create_request(request: Request):
        data = await parse_body(request, GenerateRequest)
        job, is_new = await store.reserve(data)
        if is_new:
            try:
                answer = await agent_http.post(
                    f"{settings.agent_base_url}/api/v1/generate",
                    json=data.model_dump(mode="json"),
                    timeout=10,
                )
                accepted = answer.status_code == 202 and answer.json().get("messageId") == data.messageId
            except (httpx.HTTPError, ValueError, AttributeError):
                accepted = False
            if accepted:
                await store.agent_accepted(job)
            else:
                await store.agent_rejected(job)
        else:
            accepted = await job.agent_ack
        if not accepted:
            raise DomainError(503, "SERVICE_UNAVAILABLE", "Агентный сервис не принял задание")
        message_id = data.messageId
        return {
            "messageId": message_id,
            "status": "accepted",
            "statusUrl": f"/api/v1/requests/{message_id}",
            "eventsUrl": f"/api/v1/requests/{message_id}/events",
        }

    @app.get("/api/v1/requests/{message_id}")
    async def get_request(message_id: str):
        message_id = validate_id(message_id)
        job = await store.get(message_id)
        return job.public_status()

    @app.post("/api/v1/messages/{message_id}/manifest")
    async def receive_manifest(message_id: str, request: Request):
        message_id = validate_id(message_id)
        data = await parse_body(request, Manifest)
        status = await store.record_manifest(message_id, data)
        return JSONResponse(data.model_dump(mode="json"), status_code=status)

    @app.post("/api/v1/segments", status_code=202)
    async def receive_segment(request: Request):
        data = await parse_body(request, Segment)
        await store.record_segment(data)
        return {"messageId": data.messageId, "segmentNumber": data.segmentNumber, "status": "received"}

    @app.post("/api/v1/messages/{message_id}/failure", status_code=204)
    async def receive_failure(message_id: str, request: Request):
        message_id = validate_id(message_id)
        data = await parse_body(request, Failure)
        await store.report_failure(message_id, data.code, data.message)
        return Response(status_code=204)

    @app.get("/api/v1/requests/{message_id}/events")
    async def request_events(message_id: str, request: Request):
        message_id = validate_id(message_id)
        job = await store.get(message_id)
        cursor = request.headers.get("last-event-id", "")
        if cursor and (not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 9):
            raise DomainError(422, "VALIDATION_ERROR", "Некорректный Last-Event-ID")
        index = int(cursor) if cursor else 0
        if index > len(job.events):
            raise DomainError(422, "VALIDATION_ERROR", "Last-Event-ID опережает журнал")
        if job.status in TERMINAL and index == len(job.events):
            return Response(status_code=204)

        async def stream():
            nonlocal index
            job.subscribers += 1
            try:
                yield "retry: 2000\n\n"
                while True:
                    async with job.condition:
                        if index < len(job.events):
                            pending = job.events[index:]
                        elif job.status in TERMINAL:
                            return
                        else:
                            pending = []
                            try:
                                await asyncio.wait_for(job.condition.wait(), timeout=10)
                            except TimeoutError:
                                pass
                    if not pending:
                        yield ": heartbeat\n\n"
                        continue
                    for event in pending:
                        yield (
                            f"id: {event.id}\nevent: {event.name}\n"
                            f"data: {json.dumps(event.data, ensure_ascii=False, separators=(',', ':'))}\n\n"
                        )
                        index = event.id
            finally:
                job.subscribers -= 1

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/health")
    async def health():
        kafka_ready = await bus.check_ready()
        store._purge()
        body = {
            "service": "transport",
            "status": "ok" if kafka_ready else "degraded",
            "dependencies": {"kafka": "up" if kafka_ready else "down", "ollama": "not_used"},
            "activeConnections": sum(job.subscribers for job in store.jobs.values()),
            "pendingRequests": sum(job.status not in TERMINAL for job in store.jobs.values()),
            "configuration": {
                "segmentSizeBytes": 512,
                "lossPercent": settings.loss_percent,
                "assemblyIntervalSeconds": settings.assembly_interval_seconds,
                "maxIdleCycles": settings.max_idle_cycles,
                "generationTimeoutSeconds": settings.generation_timeout_seconds,
            },
        }
        return JSONResponse(body, status_code=200 if kafka_ready else 503)

    return app


app = create_app()
