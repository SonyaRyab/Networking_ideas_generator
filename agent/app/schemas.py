"""Wire-схемы совместимы с origin/transport:transport/app/schemas.py."""

import re
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AGENT_SENDER = "content-ideas-agent"
GROUP_PLANS = {
    10: (3, 3, 4),
    11: (3, 4, 4),
    12: (4, 4, 4),
    13: (3, 3, 3, 4),
    14: (3, 3, 4, 4),
    15: (3, 4, 4, 4),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def valid_unicode(cls, value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("Недопустимый символ Unicode") from exc
        return value


class ContentType(str, Enum):
    blog = "blog"
    video = "video"
    post = "post"
    post_series = "post_series"
    other = "other"


def check_uuid4(value: str) -> str:
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value.lower():
        raise ValueError("Требуется UUID v4 в стандартной записи")
    return str(parsed)


def check_timestamp(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("Требуется время RFC3339 с часовым поясом")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


class GenerateRequest(StrictModel):
    messageId: str = Field(json_schema_extra={"format": "uuid"})
    sender: str = Field(min_length=1, max_length=50)
    sentAt: str = Field(json_schema_extra={"format": "date-time"})
    topic: str = Field(min_length=1, max_length=500)
    contentType: ContentType
    requirements: str = Field(default="", max_length=2000)
    ideaCount: int = Field(default=12, ge=10, le=15, strict=True)

    @field_validator("messageId")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return check_uuid4(value)

    @field_validator("sentAt")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return check_timestamp(value)

    @field_validator("sender", "topic", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class Idea(StrictModel):
    number: int = Field(ge=1, le=15, strict=True)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=600)

    @field_validator("title", "description")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Текст идеи не должен быть пустым")
        return value


class IdeaGroup(StrictModel):
    groupNumber: int = Field(ge=1, le=4, strict=True)
    ideas: list[Idea] = Field(min_length=3, max_length=4)


class ModelGroup(IdeaGroup):
    theme: str = Field(min_length=1, max_length=120)

    @field_validator("theme")
    @classmethod
    def nonblank_theme(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Тема группы не должна быть пустой")
        return value


class ModelOutput(StrictModel):
    groups: list[ModelGroup] = Field(min_length=3, max_length=4)

    @model_validator(mode="after")
    def distinct_themes(self):
        themes = [" ".join(group.theme.split()).casefold() for group in self.groups]
        if len(set(themes)) != len(themes):
            raise ValueError("Названия тематических групп должны различаться")
        return self


class GenerationResult(StrictModel):
    messageId: str
    sender: Literal["content-ideas-agent"] = AGENT_SENDER
    sentAt: str
    topic: str = Field(min_length=1, max_length=500)
    contentType: ContentType
    ideaCount: int = Field(ge=10, le=15, strict=True)
    groups: list[IdeaGroup] = Field(min_length=3, max_length=4)

    @field_validator("messageId")
    @classmethod
    def valid_id(cls, value: str) -> str:
        return check_uuid4(value)

    @field_validator("sentAt")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return check_timestamp(value)

    @model_validator(mode="after")
    def consistent(self):
        plan = GROUP_PLANS[self.ideaCount]
        if tuple(len(group.ideas) for group in self.groups) != plan:
            raise ValueError(f"Требуются размеры групп {plan}")
        if [g.groupNumber for g in self.groups] != list(range(1, len(plan) + 1)):
            raise ValueError("Группы должны быть пронумерованы от 1 без пропусков")
        ideas = [idea for group in self.groups for idea in group.ideas]
        if [idea.number for idea in ideas] != list(range(1, self.ideaCount + 1)):
            raise ValueError("Идеи должны быть пронумерованы от 1 без пропусков")
        titles = [" ".join(idea.title.split()).casefold() for idea in ideas]
        if len(set(titles)) != len(titles):
            raise ValueError("Заголовки идей должны различаться")
        return self


class Manifest(StrictModel):
    sentAt: str
    totalSegments: int = Field(ge=1, le=1000, strict=True)
    totalBytes: int = Field(ge=1, le=512000, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    encoding: Literal["utf-8"] = "utf-8"


class Segment(StrictModel):
    messageId: str
    sender: Literal["content-ideas-agent"] = AGENT_SENDER
    sentAt: str
    totalSegments: int = Field(ge=1, le=1000, strict=True)
    segmentNumber: int = Field(ge=1, le=1000, strict=True)
    payload: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_payload(self):
        if self.segmentNumber > self.totalSegments or len(self.payload.encode("utf-8")) > 512:
            raise ValueError("Некорректный номер или размер сегмента")
        return self


class Failure(StrictModel):
    code: Literal["MODEL_UNAVAILABLE", "GENERATION_TIMEOUT", "GENERATION_FAILED", "DELIVERY_FAILED"]
    message: str = Field(min_length=1, max_length=500)
