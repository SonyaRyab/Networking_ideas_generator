import asyncio
import copy
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as SchemaValidationError
from openapi_spec_validator import validate

from app.config import Settings
from app.delivery import prepare_delivery, split_utf8
from app.main import create_app
from app.prompts import output_schema
from app.schemas import GROUP_PLANS, GenerateRequest, GenerationResult


def request_data(**changes):
    return {
        "messageId": str(uuid4()),
        "sender": "Кирилл",
        "sentAt": "2026-11-22T17:00:00+03:00",
        "topic": "Учёба в университете",
        "contentType": "video",
        "requirements": "Для первокурсников",
        "ideaCount": 12,
        **changes,
    }


def model_output(count=12, *, themes=False):
    number = 0
    groups = []
    for index, size in enumerate(GROUP_PLANS[count], 1):
        ideas = []
        for _ in range(size):
            number += 1
            ideas.append(
                {
                    "number": number,
                    "title": f"Идея {number} 🎓",
                    "description": "Практический разбор темы для студентов.",
                }
            )
        group = {"groupNumber": index, "ideas": ideas}
        if themes:
            group["theme"] = f"Направление {index}"
        groups.append(group)
    return {"groups": groups}


class Upstream:
    def __init__(self):
        self.calls = []
        self.outputs = [model_output(themes=True)]
        self.model_status = 200
        self.tags = None
        self.manifest_status = 201
        self.manifest_responses = []
        self.segment_status = 202
        self.failure_status = 204
        self.delay = 0

    async def __call__(self, request):
        payload = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, payload, time.monotonic()))
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": self.tags if self.tags is not None else [{"name": Settings.ollama_model}]},
            )
        if path == "/api/generate":
            if self.delay:
                await asyncio.sleep(self.delay)
            output = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
            value = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else output
            return httpx.Response(
                self.model_status, json={"response": value, "done": True, "done_reason": "stop"}
            )
        if path.endswith("/manifest"):
            status = self.manifest_responses.pop(0) if self.manifest_responses else self.manifest_status
            return httpx.Response(status, json=payload)
        if path == "/api/v1/segments":
            return httpx.Response(self.segment_status, json={"status": "received"})
        if path.endswith("/failure"):
            return httpx.Response(self.failure_status)
        raise AssertionError(path)


@pytest.fixture
def rig():
    upstream = Upstream()
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(Settings(segment_delay_ms=100), http)
    with TestClient(app) as client:
        yield client, app, upstream
    asyncio.run(http.aclose())


def wait_terminal(client, message_id, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/generations/{message_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError("Background job did not finish")


@pytest.mark.parametrize("count", range(10, 16))
def test_group_plans_and_utf8_integrity(count):
    request = GenerateRequest(**request_data(ideaCount=count))
    result = GenerationResult(**request.model_dump(exclude={"sender", "requirements"}), **model_output(count))
    manifest, segments = prepare_delivery(result)
    raw = "".join(s.payload for s in segments).encode("utf-8")
    assert all(0 < len(s.payload.encode("utf-8")) <= 512 for s in segments)
    assert [s.segmentNumber for s in segments] == list(range(1, len(segments) + 1))
    assert len(raw) == manifest.totalBytes
    assert hashlib.sha256(raw).hexdigest() == manifest.sha256
    assert GenerationResult.model_validate_json(raw) == result
    assert all(s.sentAt == request.sentAt and s.messageId == request.messageId for s in segments)
    assert "requirements" not in json.loads(raw)


def test_utf8_boundaries():
    text = "я" * 255 + "🎓" + '\\"\n' + "а" * 513
    parts = split_utf8(text)
    assert "".join(parts) == text
    assert len(parts[0].encode()) == 510
    assert all(len(p.encode()) <= 512 for p in parts)


@pytest.mark.parametrize("count", range(10, 16))
def test_model_grammar_requires_exact_group_slots(count):
    schema = output_schema(GenerateRequest(**request_data(ideaCount=count)))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    output = model_output(count, themes=True)
    validator.validate(output)
    output["groups"][-1]["ideas"].pop()
    with pytest.raises(SchemaValidationError):
        validator.validate(output)


@pytest.mark.parametrize("count", range(10, 16))
def test_model_grammar_requires_exact_numbers(count):
    schema = output_schema(GenerateRequest(**request_data(ideaCount=count)))
    output = model_output(count, themes=True)
    output["groups"][0]["ideas"][0]["number"] = 2
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(schema).validate(output)


def test_background_delivery_order_and_idempotency(rig):
    client, app, upstream = rig
    data = request_data()
    upstream.delay = 0.1
    response = client.post("/api/v1/generate", json=data)
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    # Пока генерация выполняется, повтор не создаёт вторую задачу.
    assert client.post("/api/v1/generate", json=data).json() == response.json()
    changed = dict(data, topic="Другая тема")
    assert client.post("/api/v1/generate", json=changed).status_code == 409
    status = wait_terminal(client, data["messageId"])
    assert status["status"] == "completed"
    assert status["sentSegments"] == status["totalSegments"] > 1
    paths = [c[0] for c in upstream.calls]
    assert paths.count("/api/generate") == 1
    manifest_index = next(i for i, p in enumerate(paths) if p.endswith("/manifest"))
    assert manifest_index == 1
    calls = [c for c in upstream.calls if c[0] == "/api/v1/segments"]
    assert len(calls) == status["totalSegments"]  # Ни одного повтора подтверждённого 202.
    assert all(b[2] - a[2] >= 0.095 for a, b in zip(calls, calls[1:]))
    assert client.post("/api/v1/generate", json=data).status_code == 202
    assert [c[0] for c in upstream.calls].count("/api/generate") == 1


def test_normalized_repeat_and_defaults(rig):
    client, _, upstream = rig
    data = request_data(sender=" Кирилл ", topic=" Учёба ")
    del data["ideaCount"]
    del data["requirements"]
    assert client.post("/api/v1/generate", json=data).status_code == 202
    data.update(sender="Кирилл", topic="Учёба", ideaCount=12, requirements="")
    assert client.post("/api/v1/generate", json=data).status_code == 202
    assert wait_terminal(client, data["messageId"])["status"] == "completed"
    assert [p for p, _, _ in upstream.calls].count("/api/generate") == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"sender": " "},
        {"sender": "а" * 51},
        {"topic": "\t"},
        {"topic": "я" * 501},
        {"requirements": "я" * 2001},
        {"requirements": None},
        {"ideaCount": None},
        {"ideaCount": "12"},
        {"ideaCount": True},
        {"ideaCount": 12.0},
        {"ideaCount": 9},
        {"ideaCount": 16},
        {"messageId": str(uuid4()).replace("-", "")},
        {"messageId": "not-a-uuid"},
        {"messageId": "00000000-0000-1000-8000-000000000000"},
        {"sentAt": "2026-11-22T17:00:00"},
        {"sentAt": "2026-99-22T17:00:00Z"},
        {"sentAt": "2026-11-22"},
        {"contentType": "Видео"},
        {"callbackUrl": "http://wrong.invalid"},
        {"topic": "\ud800"},
    ],
)
def test_invalid_fields(rig, changes):
    client, _, upstream = rig
    data = json.dumps(request_data(**changes))
    response = client.post("/api/v1/generate", content=data, headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert not upstream.calls


@pytest.mark.parametrize(
    "content,ctype,status",
    [
        ("{", "application/json", 400),
        (b"\xff", "application/json", 400),
        ('{"a":1,"a":2}', "application/json", 400),
        ('{"x":NaN}', "application/json", 400),
        ("{}", "text/plain", 415),
        (" " * 65537, "application/json", 413),
    ],
)
def test_http_errors(rig, content, ctype, status):
    client, _, _ = rig
    response = client.post("/api/v1/generate", content=content, headers={"content-type": ctype})
    assert response.status_code == status
    assert "code" in response.json()


def test_streamed_body_limit(rig):
    client, _, _ = rig
    response = client.post(
        "/api/v1/generate", content=iter([b" " * 32768] * 3), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


def test_invalid_model_json_is_retried_before_manifest(rig):
    client, _, upstream = rig
    upstream.outputs = ["not-json", model_output(themes=True)]
    data = request_data()
    client.post("/api/v1/generate", json=data)
    assert wait_terminal(client, data["messageId"])["status"] == "completed"
    model_calls = [body for path, body, _ in upstream.calls if path == "/api/generate"]
    assert len(model_calls) == 2
    assert "Предыдущий ответ" in model_calls[1]["prompt"]


@pytest.mark.parametrize(
    "kind", ["number", "groups", "duplicate", "blank", "long", "extra", "theme_blank", "theme_duplicate"]
)
def test_invalid_model_structure_reports_failure_without_segments(rig, kind):
    client, _, upstream = rig
    output = copy.deepcopy(model_output(themes=True))
    first = output["groups"][0]["ideas"][0]
    if kind == "number":
        first["number"] = 2
    elif kind == "groups":
        output["groups"][0]["ideas"].pop()
    elif kind == "duplicate":
        first["title"] = output["groups"][0]["ideas"][1]["title"]
    elif kind == "blank":
        first["description"] = "  "
    elif kind == "long":
        first["description"] = "а" * 601
    elif kind == "theme_blank":
        output["groups"][0]["theme"] = " "
    elif kind == "theme_duplicate":
        output["groups"][0]["theme"] = output["groups"][1]["theme"]
    else:
        first["callbackUrl"] = "http://wrong.invalid"
    upstream.outputs = [output]
    data = request_data()
    client.post("/api/v1/generate", json=data)
    status = wait_terminal(client, data["messageId"])
    assert status["status"] == "failed"
    assert status["error"]["code"] == "GENERATION_FAILED"
    assert status["failureNotified"] is True
    assert not any(path.endswith("/manifest") or path.endswith("/segments") for path, _, _ in upstream.calls)


def test_missing_model_reports_model_unavailable(rig):
    client, _, upstream = rig
    upstream.model_status = 404
    data = request_data()
    assert client.post("/api/v1/generate", json=data).status_code == 202
    status = wait_terminal(client, data["messageId"])
    assert status["error"]["code"] == "MODEL_UNAVAILABLE"
    assert status["failureNotified"] is True


@pytest.mark.parametrize(
    "error,code", [(httpx.ConnectError, "MODEL_UNAVAILABLE"), (httpx.ReadTimeout, "GENERATION_TIMEOUT")]
)
def test_model_network_failure_and_health(error, code):
    async def unavailable(request):
        if request.url.path.endswith("/failure"):
            return httpx.Response(204)
        raise error("Dependency is unavailable", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
    app = create_app(Settings(), http)
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 503
        assert health.json()["dependencies"]["ollama"] == "down"
        data = request_data()
        assert client.post("/api/v1/generate", json=data).status_code == 202
        assert wait_terminal(client, data["messageId"])["error"]["code"] == code
    asyncio.run(http.aclose())


def test_parallel_jobs_keep_metadata_and_segments_separate(rig):
    client, _, upstream = rig
    upstream.delay = 0.05
    first, second = request_data(topic="Тема один"), request_data(topic="Тема два")
    assert client.post("/api/v1/generate", json=first).status_code == 202
    assert client.post("/api/v1/generate", json=second).status_code == 202
    assert wait_terminal(client, first["messageId"])["status"] == "completed"
    assert wait_terminal(client, second["messageId"])["status"] == "completed"
    model_calls = [call for call in upstream.calls if call[0] == "/api/generate"]
    assert model_calls[1][2] - model_calls[0][2] >= 0.045
    for request in (first, second):
        parts = [
            body
            for path, body, _ in upstream.calls
            if path == "/api/v1/segments" and body["messageId"] == request["messageId"]
        ]
        raw = "".join(body["payload"] for body in parts)
        result = GenerationResult.model_validate_json(raw)
        assert result.messageId == request["messageId"] and result.topic == request["topic"]


def test_delivery_conflict_does_not_send_segments(rig):
    client, _, upstream = rig
    upstream.manifest_status = 409
    data = request_data()
    client.post("/api/v1/generate", json=data)
    status = wait_terminal(client, data["messageId"])
    assert status["error"]["code"] == "DELIVERY_FAILED"
    assert not any(p == "/api/v1/segments" for p, _, _ in upstream.calls)


def test_manifest_temporary_failure_repeats_identical_body(rig):
    client, _, upstream = rig
    upstream.manifest_responses = [503, 201]
    data = request_data()
    client.post("/api/v1/generate", json=data)
    assert wait_terminal(client, data["messageId"])["status"] == "completed"
    attempts = [body for path, body, _ in upstream.calls if path.endswith("/manifest")]
    assert len(attempts) == 2 and attempts[0] == attempts[1]


def test_unreachable_failure_endpoint_is_visible(rig):
    client, _, upstream = rig
    upstream.model_status, upstream.failure_status = 503, 503
    data = request_data()
    client.post("/api/v1/generate", json=data)
    assert wait_terminal(client, data["messageId"])["failureNotified"] is False


def test_deadline_and_bounded_queue():
    upstream = Upstream()
    upstream.delay = 1
    settings = replace(Settings(), generation_timeout_seconds=0.05, max_pending_jobs=1)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(settings, http)
    with TestClient(app) as client:
        data = request_data()
        assert client.post("/api/v1/generate", json=data).status_code == 202
        assert client.post("/api/v1/generate", json=request_data()).status_code == 503
        status = wait_terminal(client, data["messageId"])
        assert status["error"]["code"] == "GENERATION_TIMEOUT"
        assert not any(path.endswith("/manifest") for path, _, _ in upstream.calls)
    asyncio.run(http.aclose())


def test_ttl_and_unknown_id(rig):
    client, app, _ = rig
    assert client.get(f"/api/v1/generations/{uuid4()}").status_code == 404
    assert client.get("/api/v1/generations/invalid").status_code == 422
    data = request_data()
    client.post("/api/v1/generate", json=data)
    wait_terminal(client, data["messageId"])
    app.state.store.jobs[data["messageId"]].terminal_clock -= 901
    assert client.get(f"/api/v1/generations/{data['messageId']}").status_code == 404


def test_health_and_openapi(rig):
    client, _, upstream = rig
    assert client.get("/api/v1/health").status_code == 200
    upstream.tags = []
    response = client.get("/api/v1/health")
    assert response.status_code == 503
    assert response.json()["dependencies"] == {
        "ollama": "up",
        "model": Settings.ollama_model,
        "modelReady": False,
    }
    spec = client.get("/openapi.json").json()
    assert spec["openapi"] == "3.0.3"
    validate(spec)
    assert spec == yaml.safe_load((Path(__file__).parents[1] / "agent.openapi.yaml").read_text())
    assert client.get("/docs").status_code == 200


def test_shutdown_cancels_background_work():
    upstream = Upstream()
    upstream.delay = 60
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app = create_app(Settings(), http)
    with TestClient(app) as client:
        client.post("/api/v1/generate", json=request_data())
    assert not app.state.store.tasks
    asyncio.run(http.aclose())
