"""Сериализация, побайтовая сегментация UTF-8 и доставка манифеста перед сегментами."""

import asyncio
import hashlib
import json

import httpx

from .config import Settings
from .ollama import AgentFailure
from .schemas import Failure, GenerationResult, Manifest, Segment


def split_utf8(text: str, max_bytes: int = 512) -> list[str]:
    if max_bytes < 4:
        raise ValueError("Размер сегмента должен вмещать любой символ UTF-8")
    segments, buffer, size = [], [], 0
    for character in text:
        width = len(character.encode("utf-8"))
        if size + width > max_bytes:
            segments.append("".join(buffer))
            buffer, size = [], 0
        buffer.append(character)
        size += width
    if buffer:
        segments.append("".join(buffer))
    return segments


def prepare_delivery(result: GenerationResult) -> tuple[Manifest, list[Segment]]:
    text = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    raw = text.encode("utf-8")
    parts = split_utf8(text)
    manifest = Manifest(
        sentAt=result.sentAt,
        totalSegments=len(parts),
        totalBytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    segments = [
        Segment(
            messageId=result.messageId,
            sentAt=result.sentAt,
            totalSegments=len(parts),
            segmentNumber=i,
            payload=part,
        )
        for i, part in enumerate(parts, 1)
    ]
    return manifest, segments


class Delivery:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    async def post(self, path: str, body: dict, expected: tuple[int, ...]) -> httpx.Response:
        for attempt in range(self.settings.delivery_attempts):
            try:
                response = await self.http.post(
                    self.settings.transport_base_url + path,
                    json=body,
                    timeout=self.settings.delivery_timeout_seconds,
                )
                if response.status_code in expected:
                    return response  # В частности, 202 никогда не повторяется из-за учебной потери.
                if response.status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if attempt + 1 < self.settings.delivery_attempts:
                await asyncio.sleep(0.1)
        raise AgentFailure("DELIVERY_FAILED", "Транспорт не подтвердил приём служебного сообщения")

    async def send(self, result: GenerationResult, progress) -> int:
        manifest, segments = prepare_delivery(result)
        await self.post(
            f"/api/v1/messages/{result.messageId}/manifest",
            manifest.model_dump(),
            (200, 201),
        )
        progress(0, len(segments))
        for index, segment in enumerate(segments):
            if index:
                await asyncio.sleep(self.settings.segment_delay_ms / 1000)
            await self.post("/api/v1/segments", segment.model_dump(), (202,))
            progress(index + 1, len(segments))
        return len(segments)

    async def notify_failure(self, message_id: str, failure: Failure):
        await self.post(f"/api/v1/messages/{message_id}/failure", failure.model_dump(), (204,))
