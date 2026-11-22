"""HTTP API агента; транспорт получает 202 до окончания генерации."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import Settings
from .delivery import Delivery
from .ollama import Ollama
from .schemas import GenerateRequest, check_uuid4
from .store import TERMINAL, DomainError, JobStore

MAX_BODY_BYTES = 64 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющийся ключ JSON")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Некорректная константа JSON")


async def parse_body(request: Request) -> GenerateRequest:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise DomainError(415, "UNSUPPORTED_MEDIA_TYPE", "Требуется application/json")
    length = request.headers.get("content-length", "0")
    if length.isdecimal() and int(length) > MAX_BODY_BYTES:
        raise DomainError(413, "PAYLOAD_TOO_LARGE", "Тело запроса превышает 64 КиБ")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise DomainError(413, "PAYLOAD_TOO_LARGE", "Тело запроса превышает 64 КиБ")
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
        )
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise DomainError(400, "INVALID_JSON", "Некорректный JSON UTF-8") from exc
    try:
        return GenerateRequest.model_validate(value)
    except ValidationError as exc:
        details = [
            {"field": ".".join(map(str, error["loc"])), "message": error["msg"]} for error in exc.errors()
        ]
        raise DomainError(422, "VALIDATION_ERROR", "Ошибка полей запроса", details) from exc


def create_app(settings: Settings | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or Settings.from_environment()
    own_http = http is None
    http = http if http is not None else httpx.AsyncClient(trust_env=False)
    ollama = Ollama(settings, http)
    store = JobStore(settings, ollama, Delivery(settings, http))

    async def cleanup():
        while True:
            await asyncio.sleep(30)
            store.purge()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ticker = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            await store.close()
            if own_http:
                await http.aclose()

    app = FastAPI(title="Content Ideas Agent", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.ollama = ollama

    def exact_openapi():
        if app.openapi_schema is None:
            source = Path(__file__).resolve().parents[1] / "agent.openapi.yaml"
            app.openapi_schema = yaml.safe_load(source.read_text(encoding="utf-8"))
        return app.openapi_schema

    app.openapi = exact_openapi

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, error: DomainError):
        body = {"code": error.code, "message": error.message}
        if error.details:
            body["details"] = error.details
        return JSONResponse(body, status_code=error.status)

    @app.post("/api/v1/generate", status_code=202)
    async def generate(request: Request):
        data = await parse_body(request)
        store.accept(data)
        return {
            "messageId": data.messageId,
            "status": "accepted",
            "statusUrl": f"/api/v1/generations/{data.messageId}",
        }

    @app.get("/api/v1/generations/{message_id}")
    async def generation_status(message_id: str):
        try:
            message_id = check_uuid4(message_id)
        except ValueError as exc:
            raise DomainError(422, "VALIDATION_ERROR", "Некорректный UUID v4") from exc
        return store.get(message_id).public_status()

    @app.get("/api/v1/health")
    async def health():
        dependencies = await ollama.readiness()
        store.purge()
        ready = dependencies["modelReady"]
        body = {
            "service": "agent",
            "status": "ok" if ready else "degraded",
            "dependencies": dependencies,
            "pendingRequests": sum(job.status not in TERMINAL for job in store.jobs.values()),
            "configuration": {
                "segmentSizeBytes": 512,
                "segmentDelayMs": settings.segment_delay_ms,
                "generationTimeoutSeconds": settings.generation_timeout_seconds,
                "maxConcurrentGenerations": settings.max_concurrent_generations,
            },
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    return app


app = create_app()
