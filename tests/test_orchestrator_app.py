"""The request path jarvis-backend hits: orchestrator HTTP -> pool -> agent.

The pool is a fake and the agent proxy calls (`_agent_exec` / `_agent_read`)
are monkeypatched, so this exercises auth, claim-error mapping and response
shaping without a cluster or a real agent.
"""

import os

import httpx

os.environ.setdefault("SANDBOX_ROLE", "orchestrator")
os.environ.setdefault("SANDBOX_IMAGE", "jarvis-sandbox:test")
os.environ["INTERNAL_API_KEY"] = "test-key"

from app.core.config import settings  # noqa: E402
from app.orchestrator import app as orch_app  # noqa: E402
from app.orchestrator.pool import CapacityError  # noqa: E402

settings.INTERNAL_API_KEY = "test-key"

AUTH = {"X-Internal-Api-Key": "test-key"}
_REQ = httpx.Request("GET", "http://agent/")


class StubPool:
    def __init__(self):
        self.released = []
        self.touched = []
        self.get_result = ("10.0.0.5", 8000, "tok")

    async def claim(self, thread_id):
        if thread_id == "full":
            raise CapacityError("all 6 sandbox slots are in use")
        return "10.0.0.5", 8000, "tok"

    def touch(self, thread_id):
        self.touched.append(thread_id)

    async def get(self, thread_id):
        return self.get_result

    async def release(self, thread_id):
        self.released.append(thread_id)

    def snapshot(self):
        return {"threads": {}}


def make_client(pool):
    app = orch_app.build_app()
    app.state.pool = pool
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_exec_requires_auth():
    async with make_client(StubPool()) as c:
        r = await c.post("/api/v1/sandbox/exec", json={"thread_id": "a", "command": "echo hi"})
    assert r.status_code == 401


async def test_exec_proxies_to_agent(monkeypatch):
    async def fake_exec(ip, port, token, command, timeout):
        assert (ip, port, token) == ("10.0.0.5", 8000, "tok")
        return httpx.Response(
            200, json={"stdout": "hi\n", "exit_code": 0, "timed_out": False}, request=_REQ
        )

    monkeypatch.setattr(orch_app, "_agent_exec", fake_exec)
    pool = StubPool()
    async with make_client(pool) as c:
        r = await c.post(
            "/api/v1/sandbox/exec",
            json={"thread_id": "conv-1", "command": "echo hi"},
            headers=AUTH,
        )
    assert r.status_code == 200
    assert r.json()["stdout"] == "hi\n"
    assert pool.touched == ["conv-1"]


async def test_exec_capacity_returns_503(monkeypatch):
    async with make_client(StubPool()) as c:
        r = await c.post(
            "/api/v1/sandbox/exec",
            json={"thread_id": "full", "command": "echo hi"},
            headers=AUTH,
        )
    assert r.status_code == 503


async def test_exec_agent_down_returns_502(monkeypatch):
    async def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(orch_app, "_agent_exec", boom)
    async with make_client(StubPool()) as c:
        r = await c.post(
            "/api/v1/sandbox/exec",
            json={"thread_id": "conv-1", "command": "echo hi"},
            headers=AUTH,
        )
    assert r.status_code == 502


async def test_read_proxies_bytes(monkeypatch):
    async def fake_read(ip, port, token, name):
        return httpx.Response(
            200,
            content=b"PDFDATA",
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'inline; filename="r.pdf"',
            },
            request=_REQ,
        )

    monkeypatch.setattr(orch_app, "_agent_read", fake_read)
    async with make_client(StubPool()) as c:
        r = await c.get(
            "/api/v1/sandbox/read", params={"thread_id": "c", "name": "r.pdf"}, headers=AUTH
        )
    assert r.status_code == 200
    assert r.content == b"PDFDATA"
    assert r.headers["content-type"] == "application/pdf"


async def test_read_no_sandbox_returns_404():
    pool = StubPool()
    pool.get_result = None

    async def _get(_):
        return None

    pool.get = _get  # type: ignore[assignment]
    async with make_client(pool) as c:
        r = await c.get(
            "/api/v1/sandbox/read", params={"thread_id": "c", "name": "x"}, headers=AUTH
        )
    assert r.status_code == 404


async def test_reset_releases_pod():
    pool = StubPool()
    async with make_client(pool) as c:
        r = await c.post("/api/v1/sandbox/reset", json={"thread_id": "conv-1"}, headers=AUTH)
    assert r.status_code == 200
    assert pool.released == ["conv-1"]


async def test_health_open():
    async with make_client(StubPool()) as c:
        r = await c.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["role"] == "orchestrator"
