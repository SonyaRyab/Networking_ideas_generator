"""Структуры запросов и восстановленного результата."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContentType(str, Enum):
    blog = "blog"
    video = "video"
    post = "post"
    post_series = "post_series"
    other = "other"


def check_uuid4(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("Требуется UUID v4") from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise ValueError("Требуется UUID v4")
    return str(parsed)


def check_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Требуется время RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Время должно содержать часовой пояс")
    return value


class GenerateRequest(StrictModel):
    messageId: str
    sender: str = Field(min_length=1, max_length=50)
    sentAt: str
    topic: str = Field(min_length=1, max_length=500)
    contentType: ContentType
    requirements: str = Field(default="", max_length=2000)
    ideaCount: int = Field(default=12, ge=10, le=15, strict=True)

    @field_validator("messageId")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return check_uuid4(value)

    @field_validator("sentAt")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return check_timestamp(value)

    @field_validator("sender", "topic")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Не должно быть пустым")
        return value


class Manifest(StrictModel):
    sentAt: str
    totalSegments: int = Field(ge=1, le=1000, strict=True)
    totalBytes: int = Field(ge=1, le=512000, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding: str = Field(pattern=r"^utf-8$")

    @field_validator("sentAt")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return check_timestamp(value)


class Segment(StrictModel):
    messageId: str
    sender: str = Field(pattern=r"^content-ideas-agent$")
    sentAt: str
    totalSegments: int = Field(ge=1, le=1000, strict=True)
    segmentNumber: int = Field(ge=1, le=1000, strict=True)
    payload: str = Field(min_length=1, max_length=512)

    @field_validator("messageId")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return check_uuid4(value)

    @field_validator("sentAt")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return check_timestamp(value)

    @model_validator(mode="after")
    def valid_payload(self) -> "Segment":
        if self.segmentNumber > self.totalSegments:
            raise ValueError("Номер сегмента превышает totalSegments")
        if len(self.payload.encode("utf-8")) > 512:
            raise ValueError("payload превышает 512 байт UTF-8")
        return self


class Failure(StrictModel):
    code: str = Field(pattern=r"^(MODEL_UNAVAILABLE|GENERATION_TIMEOUT|GENERATION_FAILED|DELIVERY_FAILED)$")
    message: str = Field(min_length=1, max_length=500)


class Idea(StrictModel):
    number: int = Field(ge=1, le=15, strict=True)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=600)

    @field_validator("title", "description")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Пустой текст идеи")
        return value


class IdeaGroup(StrictModel):
    groupNumber: int = Field(ge=1, le=4, strict=True)
    ideas: list[Idea] = Field(min_length=3, max_length=4)


GROUP_PLANS = {
    10: (3, 3, 4),
    11: (3, 4, 4),
    12: (4, 4, 4),
    13: (3, 3, 3, 4),
    14: (3, 3, 4, 4),
    15: (3, 4, 4, 4),
}


class GenerationResult(StrictModel):
    messageId: str
    sender: str = Field(pattern=r"^content-ideas-agent$")
    sentAt: str
    topic: str = Field(min_length=1, max_length=500)
    contentType: ContentType
    ideaCount: int = Field(ge=10, le=15, strict=True)
    groups: list[IdeaGroup] = Field(min_length=3, max_length=4)

    @field_validator("messageId")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return check_uuid4(value)

    @field_validator("sentAt")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return check_timestamp(value)

    @model_validator(mode="after")
    def consistent(self) -> "GenerationResult":
        expected = GROUP_PLANS[self.ideaCount]
        if tuple(len(group.ideas) for group in self.groups) != expected:
            raise ValueError("Неверный размер групп")
        if [group.groupNumber for group in self.groups] != list(range(1, len(expected) + 1)):
            raise ValueError("Неверная нумерация групп")
        if [idea.number for group in self.groups for idea in group.ideas] != list(range(1, self.ideaCount + 1)):
            raise ValueError("Неверная нумерация идей")
        return self
