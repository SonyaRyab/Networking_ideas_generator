"""Пересобрать статическую OpenAPI 3.0.3: python scripts/export_openapi.py."""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas import Failure, GenerateRequest, GenerationResult, Manifest, Segment  # noqa: E402


def convert_schema(value):
    if isinstance(value, list):
        return [convert_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: convert_schema(item) for key, item in value.items()}
    if "const" in result:
        result["enum"] = [result.pop("const")]
    return result


def response(description, schema=None):
    result = {"description": description}
    if schema:
        result["content"] = {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}}
    return result


def build_spec():
    schemas = {}
    for model in (GenerateRequest, GenerationResult, Manifest, Segment, Failure):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[model.__name__] = schema
    schemas.update(
        {
            "Error": {
                "type": "object",
                "required": ["code", "message"],
                "additionalProperties": False,
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "details": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["field", "message"],
                            "properties": {"field": {"type": "string"}, "message": {"type": "string"}},
                        },
                    },
                },
            },
            "Accepted": {
                "type": "object",
                "required": ["messageId", "status", "statusUrl"],
                "additionalProperties": False,
                "properties": {
                    "messageId": {"type": "string", "format": "uuid"},
                    "status": {"type": "string", "enum": ["accepted"]},
                    "statusUrl": {"type": "string"},
                },
            },
            "GenerationStatus": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "messageId",
                    "status",
                    "acceptedAt",
                    "updatedAt",
                    "totalSegments",
                    "sentSegments",
                ],
                "properties": {
                    "messageId": {"type": "string", "format": "uuid"},
                    "status": {
                        "type": "string",
                        "enum": ["accepted", "generating", "sending", "completed", "failed"],
                    },
                    "acceptedAt": {"type": "string", "format": "date-time"},
                    "updatedAt": {"type": "string", "format": "date-time"},
                    "totalSegments": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "sentSegments": {"type": "integer", "minimum": 0, "maximum": 1000},
                    "error": {"$ref": "#/components/schemas/Failure"},
                    "failureNotified": {"type": "boolean"},
                },
            },
            "Health": {
                "type": "object",
                "required": ["service", "status", "dependencies", "pendingRequests", "configuration"],
                "properties": {
                    "service": {"type": "string", "enum": ["agent"]},
                    "status": {"type": "string", "enum": ["ok", "degraded"]},
                    "dependencies": {
                        "type": "object",
                        "required": ["ollama", "model", "modelReady"],
                        "properties": {
                            "ollama": {"type": "string", "enum": ["up", "down"]},
                            "model": {"type": "string"},
                            "modelReady": {"type": "boolean"},
                        },
                    },
                    "pendingRequests": {"type": "integer", "minimum": 0},
                    "configuration": {
                        "type": "object",
                        "properties": {
                            "segmentSizeBytes": {"type": "integer", "enum": [512]},
                            "segmentDelayMs": {"type": "integer", "minimum": 100, "maximum": 300},
                            "generationTimeoutSeconds": {"type": "number"},
                            "maxConcurrentGenerations": {"type": "integer", "minimum": 1},
                        },
                    },
                },
            },
        }
    )
    error_responses = {
        str(status): response(description, "Error")
        for status, description in (
            (400, "Некорректный JSON UTF-8"),
            (409, "UUID занят другим запросом"),
            (413, "Тело больше 64 КиБ"),
            (415, "Требуется application/json"),
            (422, "Нарушена схема запроса"),
            (503, "Очередь заполнена"),
        )
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Content Ideas Agent",
            "version": "1.0.0",
            "description": (
                "Агент локальной генерации через Ollama. После 202 регистрирует манифест и отправляет "
                "сегменты транспорту. completed означает завершение HTTP-отправки, а не сборки ответа. "
                "Одинаковый нормализованный запрос с занятым UUID идемпотентен; иной запрос даёт 409. "
                "Диагностика хранится 15 минут после завершения и исчезает при перезапуске."
            ),
        },
        "servers": [{"url": "/"}],
        "paths": {
            "/api/v1/generate": {
                "post": {
                    "operationId": "generateIdeas",
                    "summary": "Принять задание в фоновую обработку",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/GenerateRequest"},
                                "example": {
                                    "messageId": "8d964744-5b5c-4c3b-91f1-6f54e325c979",
                                    "sender": "Кирилл",
                                    "sentAt": "2026-11-22T17:00:00+03:00",
                                    "topic": "Учёба в университете",
                                    "contentType": "video",
                                    "requirements": "Для первокурсников",
                                    "ideaCount": 12,
                                },
                            }
                        },
                    },
                    "responses": {"202": response("Задание принято", "Accepted"), **error_responses},
                }
            },
            "/api/v1/generations/{messageId}": {
                "get": {
                    "operationId": "getGeneration",
                    "summary": "Проверить генерацию и отправку",
                    "parameters": [
                        {
                            "name": "messageId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        }
                    ],
                    "responses": {
                        "200": response("Состояние задания", "GenerationStatus"),
                        "404": response("Неизвестное или удалённое задание", "Error"),
                        "422": response("Некорректный UUID v4", "Error"),
                    },
                }
            },
            "/api/v1/health": {
                "get": {
                    "operationId": "getHealth",
                    "summary": "Проверить Ollama и наличие выбранной модели",
                    "responses": {
                        "200": response("Ollama доступна, модель установлена", "Health"),
                        "503": response("Ollama недоступна или модель отсутствует", "Health"),
                    },
                }
            },
        },
        "components": {"schemas": convert_schema(schemas)},
    }


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "agent.openapi.yaml"
    target.write_text(yaml.safe_dump(build_spec(), allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(target)
