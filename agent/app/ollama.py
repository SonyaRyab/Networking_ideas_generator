"""Асинхронное обращение к локальной модели с ограниченной коррекцией JSON."""

import httpx
from pydantic import ValidationError

from .config import Settings
from .prompts import SYSTEM_PROMPT, build_prompt, output_schema
from .schemas import Failure, GenerateRequest, GenerationResult, IdeaGroup, ModelOutput


class AgentFailure(Exception):
    def __init__(self, code: str, message: str):
        self.failure = Failure(code=code, message=message)
        super().__init__(message)


class Ollama:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    async def readiness(self) -> dict:
        model = self.settings.ollama_model
        try:
            response = await self.http.get(f"{self.settings.ollama_base_url}/api/tags", timeout=3)
            response.raise_for_status()
            models = response.json()["models"]
            canonical = model if ":" in model else model + ":latest"
            ready = any(item.get("name") == canonical or item.get("model") == canonical for item in models)
            return {"ollama": "up", "model": model, "modelReady": ready}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return {"ollama": "down", "model": model, "modelReady": False}

    async def generate(self, request: GenerateRequest) -> GenerationResult:
        correction = None
        for attempt in range(self.settings.model_attempts):
            try:
                response = await self.http.post(
                    f"{self.settings.ollama_base_url}/api/generate",
                    json={
                        "model": self.settings.ollama_model,
                        "system": SYSTEM_PROMPT,
                        "prompt": build_prompt(request, correction),
                        "stream": False,
                        "format": output_schema(request),
                        "keep_alive": self.settings.keep_alive,
                        "options": {
                            "temperature": self.settings.temperature if attempt == 0 else 0.2,
                            "num_ctx": self.settings.num_ctx,
                            "num_predict": self.settings.num_predict,
                        },
                    },
                    timeout=httpx.Timeout(self.settings.generation_timeout_seconds, connect=5),
                )
            except httpx.TimeoutException as exc:
                raise AgentFailure("GENERATION_TIMEOUT", "Модель не ответила за отведённое время") from exc
            except httpx.HTTPError as exc:
                raise AgentFailure("MODEL_UNAVAILABLE", "Не удалось связаться с локальной Ollama") from exc
            if response.status_code != 200:
                code = "MODEL_UNAVAILABLE" if response.status_code in (404, 502, 503) else "GENERATION_FAILED"
                raise AgentFailure(code, "Ollama не смогла выполнить генерацию; проверьте модель и её журнал")
            try:
                body = response.json()
                if body.get("done") is not True or body.get("done_reason") == "length":
                    raise ValueError("Ответ модели не завершён; сократи описания")
                output = ModelOutput.model_validate_json(body["response"])
                metadata = request.model_dump(exclude={"sender", "requirements"})
                groups = [IdeaGroup(groupNumber=g.groupNumber, ideas=g.ideas) for g in output.groups]
                return GenerationResult(**metadata, groups=groups)
            except ValidationError as exc:
                # Не включать текст пользователя или ответ модели в уточняющий промпт/журнал.
                correction = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())[
                    :1200
                ]
            except (ValueError, KeyError, TypeError, AttributeError):
                correction = (
                    "Нужен завершённый JSON с groups, groupNumber, theme, ideas, number, title, description."
                )
        raise AgentFailure(
            "GENERATION_FAILED", "Модель не сформировала корректный результат за допустимое число попыток"
        )
