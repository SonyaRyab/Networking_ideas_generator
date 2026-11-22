"""Реальная Ollama + HTTP API агента + проверяющий приёмник вместо транспорта/Kafka.

python scripts/smoke_local.py --ollama-url http://127.0.0.1:11434 --count 12
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.schemas import GenerationResult, Manifest, Segment  # noqa: E402


async def run(args):
    manifest, segments, failures, model_times, model_responses = None, [], [], [], []
    async with httpx.AsyncClient(trust_env=False, timeout=args.timeout) as real_http:

        async def route(request):
            nonlocal manifest
            if request.url.host != "receiver.test":
                started = time.monotonic()
                response = await real_http.send(request)
                if request.url.path == "/api/generate":
                    body = response.json()
                    model_responses.append(body.get("response"))
                    model_times.append(
                        {
                            "seconds": round(time.monotonic() - started, 2),
                            "evalCount": body.get("eval_count"),
                            "doneReason": body.get("done_reason"),
                        }
                    )
                return response
            body = json.loads(request.content)
            if request.url.path.endswith("/manifest"):
                manifest = Manifest(**body)
                return httpx.Response(201, json=body)
            if request.url.path.endswith("/segments"):
                assert manifest is not None
                segments.append(Segment(**body))
                return httpx.Response(202, json={"status": "received"})
            if request.url.path.endswith("/failure"):
                failures.append(body)
                return httpx.Response(204)
            raise AssertionError(str(request.url))

        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as upstream:
            settings = Settings(
                ollama_base_url=args.ollama_url,
                ollama_model=args.model,
                transport_base_url="http://receiver.test",
                generation_timeout_seconds=args.timeout,
            )
            app = create_app(settings, upstream)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
                ) as client:
                    data = {
                        "messageId": str(uuid4()),
                        "sender": "Локальная проверка",
                        "sentAt": datetime.now(timezone.utc).isoformat(),
                        "topic": args.topic,
                        "contentType": args.content_type,
                        "requirements": args.requirements,
                        "ideaCount": args.count,
                    }
                    started = time.monotonic()
                    accepted = await client.post("/api/v1/generate", json=data)
                    assert accepted.status_code == 202, accepted.text
                    while True:
                        status = (await client.get(accepted.json()["statusUrl"])).json()
                        if status["status"] in {"completed", "failed"}:
                            break
                        assert time.monotonic() - started < args.timeout + 45
                        await asyncio.sleep(0.05)
                    report = {
                        "model": args.model,
                        "ideaCount": args.count,
                        "contentType": args.content_type,
                        "seconds": round(time.monotonic() - started, 2),
                        "modelAttempts": model_times,
                        "status": status,
                        "failureNotifications": failures,
                        "modelResponses": model_responses,
                    }
                    try:
                        running = await real_http.get(args.ollama_url.rstrip("/") + "/api/ps")
                        report["runtime"] = [
                            {key: model.get(key) for key in ("name", "size", "size_vram", "context_length")}
                            for model in running.json().get("models", [])
                            if model.get("name") == args.model
                        ]
                    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
                        report["runtime"] = []
                    if status["status"] == "completed":
                        raw = "".join(
                            s.payload for s in sorted(segments, key=lambda s: s.segmentNumber)
                        ).encode("utf-8")
                        assert manifest.totalSegments == len(segments)
                        assert manifest.totalBytes == len(raw)
                        assert manifest.sha256 == hashlib.sha256(raw).hexdigest()
                        result = GenerationResult.model_validate_json(raw)
                        assert result.messageId == data["messageId"] and result.sentAt == data["sentAt"]
                        assert (
                            result.topic == data["topic"] and result.contentType.value == data["contentType"]
                        )
                        assert result.ideaCount == args.count
                        report["result"] = result.model_dump(mode="json")
                        report["checks"] = (
                            "schema, group plan, numbering, UTF-8 <=512 bytes, manifest, SHA-256: passed"
                        )
                    output = Path(args.output)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
                    print(
                        json.dumps(
                            {k: v for k, v in report.items() if k not in {"result", "modelResponses"}},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                    print(f"Report: {output.resolve()}")
                    return 0 if status["status"] == "completed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default=Settings.ollama_model)
    parser.add_argument("--count", type=int, choices=range(10, 16), default=12)
    parser.add_argument("--topic", default="Как первокурснику освоиться в университете")
    parser.add_argument(
        "--content-type", choices=["blog", "video", "post", "post_series", "other"], default="video"
    )
    parser.add_argument(
        "--requirements", default="Для студентов 17–20 лет, доступно и с практическими примерами"
    )
    parser.add_argument("--timeout", type=float, default=110)
    parser.add_argument("--output", default="reports/smoke-local.json")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
