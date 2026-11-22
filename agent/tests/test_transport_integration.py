"""Проверка с настоящим кодом транспорта и подставной Kafka, без сетевых сервисов.

TRANSPORT_SOURCE_DIR=/path/to/transport python -m pytest tests/test_transport_integration.py
"""

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from test_agent import model_output, request_data

from app.config import Settings
from app.main import create_app

SOURCE = Path(os.getenv("TRANSPORT_SOURCE_DIR", str(Path(__file__).resolve().parents[2] / "transport")))
pytestmark = pytest.mark.skipif(
    not (SOURCE / "app/main.py").exists(), reason="Транспортная ветка не подключена"
)


def load_transport():
    name = "transport_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name,
            SOURCE / "app/__init__.py",
            submodule_search_locations=[str(SOURCE / "app")],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(name + ".main"), importlib.import_module(name + ".config")


class MemoryBus:
    async def start(self, consume):
        self.queue = asyncio.Queue()

        async def worker():
            while True:
                await consume(await self.queue.get())

        self.worker = asyncio.create_task(worker())

    async def stop(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)

    async def publish(self, key, raw):
        await self.queue.put(raw)

    async def check_ready(self):
        return True


@pytest.mark.parametrize(
    "loss,model_status,terminal", [(0, 200, "completed"), (100, 200, "failed"), (0, 404, "failed")]
)
def test_transport_agent_roundtrip(loss, model_status, terminal):
    async def scenario():
        transport_main, transport_config = load_transport()
        apps = {}
        model_calls, segment_calls = [], []

        async def route(request):
            if request.url.host == "ollama.test":
                model_calls.append(request)
                return httpx.Response(
                    model_status,
                    json={
                        "done": True,
                        "response": json.dumps(model_output(themes=True), ensure_ascii=False),
                    },
                )
            if request.url.path == "/api/v1/segments":
                segment_calls.append(request)
            target = apps[request.url.host]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=target), base_url=str(request.url)
            ) as proxy:
                return await proxy.request(
                    request.method, request.url, headers=request.headers, content=request.content
                )

        async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as upstream:
            apps["agent.test"] = create_app(
                Settings(
                    ollama_base_url="http://ollama.test",
                    transport_base_url="http://transport.test",
                    segment_delay_ms=100,
                ),
                upstream,
            )
            transport = transport_main.create_app(
                transport_config.Settings(agent_base_url="http://agent.test", loss_percent=loss),
                bus=MemoryBus(),
                agent_http=upstream,
            )
            apps["transport.test"] = transport
            async with (
                apps["agent.test"].router.lifespan_context(apps["agent.test"]),
                transport.router.lifespan_context(transport),
            ):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=transport), base_url="http://transport.test"
                ) as client:
                    data = request_data()
                    response = await client.post("/api/v1/requests", json=data)
                    assert response.status_code == 202
                    agent_store = apps["agent.test"].state.store
                    deadline = time.monotonic() + 8
                    while agent_store.get(data["messageId"]).status not in {"completed", "failed"}:
                        assert time.monotonic() < deadline
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.01)  # Дочитать очередь перед сборкой.
                    job = transport.state.store.jobs[data["messageId"]]
                    if loss == 100:
                        assert agent_store.get(data["messageId"]).status == "completed"
                        job.last_progress_at -= 10
                    await transport.state.store.tick()
                    state = (await client.get(response.json()["statusUrl"])).json()
                    assert state["status"] == terminal
                    events = (await client.get(response.json()["eventsUrl"])).text
                    if terminal == "completed":
                        assert state["result"]["ideaCount"] == 12
                        assert state["result"]["sentAt"] == data["sentAt"]
                        assert events.count("event: idea_group") == 3
                        assert "event: generation_completed" in events
                    else:
                        expected = "ASSEMBLY_TIMEOUT" if loss == 100 else "MODEL_UNAVAILABLE"
                        assert state["error"]["code"] == expected
                        assert "result" not in state
                        assert "event: generation_error" in events
                        assert "event: idea_group" not in events
                    assert len(model_calls) == 1
                    assert len(segment_calls) == agent_store.get(data["messageId"]).sent_segments

    asyncio.run(scenario())
