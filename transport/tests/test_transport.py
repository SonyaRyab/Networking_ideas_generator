"""Проверка протокола транспорта без настоящего агента и Kafka."""

import hashlib
import json
import unittest
from uuid import uuid4

import httpx

from app.config import Settings
from app.main import create_app
from app.schemas import GenerateRequest, Manifest, Segment
from app.store import DomainError, JobStore


class FakeBus:
    def __init__(self):
        self.messages = []
        self.ready = True

    async def start(self, callback):
        self.callback = callback

    async def stop(self):
        pass

    async def publish(self, key, payload):
        self.messages.append((key, payload))

    async def check_ready(self):
        return self.ready


def request_data(count=12):
    return {
        "messageId": str(uuid4()),
        "sender": " Автор ",
        "sentAt": "2026-11-22T09:00:00Z",
        "topic": " Подготовка автомобиля к зиме ",
        "contentType": "post",
        "requirements": "Для начинающих",
        "ideaCount": count,
    }


def valid_result(request):
    plans = {10: (3, 3, 4), 11: (3, 4, 4), 12: (4, 4, 4), 13: (3, 3, 3, 4), 14: (3, 3, 4, 4), 15: (3, 4, 4, 4)}
    groups = []
    number = 1
    for group_number, size in enumerate(plans[request.ideaCount], 1):
        ideas = []
        for _ in range(size):
            ideas.append({"number": number, "title": f"Идея {number}", "description": f"Описание идеи {number}"})
            number += 1
        groups.append({"groupNumber": group_number, "ideas": ideas})
    return {
        "messageId": request.messageId,
        "sender": "content-ideas-agent",
        "sentAt": request.sentAt,
        "topic": request.topic,
        "contentType": request.contentType.value,
        "ideaCount": request.ideaCount,
        "groups": groups,
    }


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 0.0
        self.bus = FakeBus()
        self.settings = Settings(loss_percent=10)
        self.store = JobStore(self.settings, self.bus, random_source=lambda: 0.5, clock=lambda: self.now)
        self.request = GenerateRequest.model_validate(request_data())
        self.job, _ = await self.store.reserve(self.request)
        await self.store.agent_accepted(self.job)

    async def register_text(self, text):
        encoded = text.encode("utf-8")
        pieces = [text[i:i+120] for i in range(0, len(text), 120)]
        manifest = Manifest(sentAt=self.request.sentAt, totalSegments=len(pieces), totalBytes=len(encoded), sha256=hashlib.sha256(encoded).hexdigest(), encoding="utf-8")
        self.assertEqual(await self.store.record_manifest(self.request.messageId, manifest), 201)
        return manifest, pieces

    async def test_complete_reordered_segments_and_sse_groups(self):
        text = json.dumps(valid_result(self.request), ensure_ascii=False, separators=(",", ":"))
        manifest, pieces = await self.register_text(text)
        for number in range(len(pieces), 0, -1):
            segment = Segment(messageId=self.request.messageId, sender="content-ideas-agent", sentAt=self.request.sentAt, totalSegments=len(pieces), segmentNumber=number, payload=pieces[number-1])
            await self.store.record_segment(segment)
        self.assertEqual(len(self.bus.messages), len(pieces))
        for _, raw in self.bus.messages:
            await self.store.consume_segment(raw)
        await self.store.tick()
        self.assertEqual(self.job.status, "completed")
        self.assertEqual([event.name for event in self.job.events], ["idea_group"] * 3 + ["generation_completed"])
        self.assertEqual([event.id for event in self.job.events], [1, 2, 3, 4])
        self.assertEqual(self.job.result["ideaCount"], 12)

    async def test_drop_once_and_assembly_timeout(self):
        self.store.random_source = lambda: 0.0
        text = json.dumps(valid_result(self.request), ensure_ascii=False, separators=(",", ":"))
        manifest, pieces = await self.register_text(text)
        segment = Segment(messageId=self.request.messageId, sender="content-ideas-agent", sentAt=self.request.sentAt, totalSegments=len(pieces), segmentNumber=1, payload=pieces[0])
        await self.store.record_segment(segment)
        await self.store.record_segment(segment)
        self.assertEqual(self.bus.messages, [])
        self.now = 9.0
        await self.store.tick()
        self.assertEqual(self.job.error["code"], "ASSEMBLY_TIMEOUT")
        self.assertEqual([event.name for event in self.job.events], ["generation_error"])

    async def test_invalid_hash_never_emits_groups(self):
        text = json.dumps(valid_result(self.request), ensure_ascii=False, separators=(",", ":"))
        manifest, pieces = await self.register_text(text)
        for number, payload in enumerate(pieces, 1):
            segment = Segment(messageId=self.request.messageId, sender="content-ideas-agent", sentAt=self.request.sentAt, totalSegments=len(pieces), segmentNumber=number, payload=payload)
            await self.store.record_segment(segment)
        for _, raw in self.bus.messages:
            broken = json.loads(raw)
            if broken["segmentNumber"] == 1:
                broken["payload"] = "X" + broken["payload"][1:]
            await self.store.consume_segment(json.dumps(broken).encode("utf-8"))
        await self.store.tick()
        self.assertEqual(self.job.status, "failed")
        self.assertEqual(self.job.error["code"], "INVALID_RESULT")
        self.assertEqual(len(self.job.events), 1)

    async def test_no_manifest_timeout_and_ttl(self):
        self.now = 120.0
        await self.store.tick()
        self.assertEqual(self.job.error["code"], "GENERATION_TIMEOUT")
        self.now = 1021.0
        with self.assertRaises(DomainError) as captured:
            await self.store.get(self.request.messageId)
        self.assertEqual(captured.exception.status, 404)

    async def test_request_and_manifest_idempotency(self):
        repeated, is_new = await self.store.reserve(self.request)
        self.assertIs(repeated, self.job)
        self.assertFalse(is_new)
        changed = self.request.model_copy(update={"topic": "Другая тема"})
        with self.assertRaises(DomainError):
            await self.store.reserve(changed)
        text = json.dumps(valid_result(self.request), ensure_ascii=False)
        manifest, _ = await self.register_text(text)
        self.assertEqual(await self.store.record_manifest(self.request.messageId, manifest), 200)


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_contract_and_replay(self):
        bus = FakeBus()
        forwarded = []

        def agent_handler(request):
            forwarded.append(json.loads(request.content))
            return httpx.Response(202, json={"messageId": forwarded[-1]["messageId"], "status": "accepted"})

        agent = httpx.AsyncClient(transport=httpx.MockTransport(agent_handler))
        app = create_app(Settings(), bus=bus, agent_http=agent)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://transport") as client:
            data = request_data()
            first = await client.post(
                "/api/v1/requests", json=data, headers={"Origin": "http://localhost:5173"}
            )
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.headers["access-control-allow-origin"], "*")
            self.assertEqual(first.json()["messageId"], data["messageId"])
            self.assertEqual(
                first.json()["eventsUrl"],
                f"/api/v1/requests/{data['messageId']}/events",
            )
            self.assertEqual((await client.post("/api/v1/requests", json=data)).status_code, 202)
            self.assertEqual(len(forwarded), 1)
            self.assertEqual(forwarded[0]["topic"], data["topic"].strip())
            invalid = await client.post("/api/v1/requests", json={**data, "topic": ""})
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual((await client.get("/api/v1/health")).status_code, 200)
            message_id = data["messageId"]
            failure = await client.post(f"/api/v1/messages/{message_id}/failure", json={"code": "GENERATION_FAILED", "message": "Модель не ответила"})
            self.assertEqual(failure.status_code, 204)
            sse = await client.get(f"/api/v1/requests/{message_id}/events")
            self.assertEqual(sse.status_code, 200)
            self.assertIn("event: generation_error", sse.text)
            self.assertEqual((await client.get(f"/api/v1/requests/{message_id}/events", headers={"Last-Event-ID": "1"})).status_code, 204)
            self.assertEqual((await client.get(f"/api/v1/requests/{message_id}/events", headers={"Last-Event-ID": "2"})).status_code, 422)
            self.assertEqual((await client.get("/openapi.json")).json()["openapi"], "3.0.3")
        await agent.aclose()


if __name__ == "__main__":
    unittest.main()
