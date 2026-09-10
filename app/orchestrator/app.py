"""HTTP surface of the orchestrator (the control plane).

Same external contract as the old shared service, so jarvis-backend is
unchanged (Service `sandbox`, `X-Internal-Api-Key`):

    POST /api/v1/sandbox/exec   {thread_id, command, timeout_seconds?}
    GET  /api/v1/sandbox/read   ?thread_id=&name=
    POST /api/v1/sandbox/reset  {thread_id}
    GET  /api/v1/health
    GET  /api/v1/admin/pods     (no auth; internal-only, for debugging)

Under the hood each `thread_id` gets its own agent pod from the warm pool
(`app.orchestrator.pool`); exec/read are proxied straight to that pod.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel

from app.api.deps import verify_internal_key
from app.core.config import settings
from app.orchestrator.k8s import K8sClient
from app.orchestrator.pool import CapacityError, ClaimTimeout, SandboxPool

logger = logging.getLogger(__name__)


class ExecRequest(BaseModel):
    thread_id: str
    command: str
    timeout_seconds: int | None = None


class ResetRequest(BaseModel):
    thread_id: str


async def _agent_exec(ip: str, port: int, token: str, command: str, timeout: int) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout + 30) as c:
        return await c.post(
            f"http://{ip}:{port}{settings.API_PREFIX}/sandbox/exec",
            json={"command": command, "timeout_seconds": timeout},
            headers={"X-Agent-Token": token},
        )


async def _agent_read(ip: str, port: int, token: str, name: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60) as c:
        return await c.get(
            f"http://{ip}:{port}{settings.API_PREFIX}/sandbox/read",
            params={"name": name},
            headers={"X-Agent-Token": token},
        )


def build_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        k8s = K8sClient(settings.POD_NAMESPACE)
        await k8s.start()
        pool = SandboxPool(k8s)
        await pool.start()
        app.state.pool = pool
        tasks = [
            asyncio.create_task(pool.reconcile_loop()),
            asyncio.create_task(pool.gc_loop()),
        ]
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await k8s.close()

    app = FastAPI(
        title=f"{settings.APP_NAME} (orchestrator)",
        version=settings.APP_VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.pool = None

    def pool() -> SandboxPool:
        p = getattr(app.state, "pool", None)
        if p is None:
            raise HTTPException(status_code=503, detail="orchestrator still starting")
        return p

    open_ = APIRouter(prefix=settings.API_PREFIX)
    guarded = APIRouter(prefix=settings.API_PREFIX, dependencies=[Depends(verify_internal_key)])

    @open_.get("/health")
    async def health():
        return {"status": "ok", "role": "orchestrator", "version": settings.APP_VERSION}

    @open_.get("/admin/pods")
    async def admin_pods():
        return pool().snapshot()

    @guarded.post("/sandbox/exec")
    async def exec_command(body: ExecRequest):
        timeout = body.timeout_seconds or settings.COMMAND_TIMEOUT_SECONDS
        try:
            ip, port, token = await pool().claim(body.thread_id)
        except CapacityError as e:
            raise HTTPException(status_code=503, detail=str(e)) from None
        except ClaimTimeout as e:
            raise HTTPException(status_code=504, detail=str(e)) from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

        try:
            resp = await _agent_exec(ip, port, token, body.command, timeout)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"sandbox agent unreachable: {e}") from None

        pool().touch(body.thread_id)
        return resp.json()

    @guarded.get("/sandbox/read")
    async def read_file(thread_id: str, name: str):
        got = await pool().get(thread_id)
        if got is None:
            raise HTTPException(status_code=404, detail="no live sandbox for this conversation")
        ip, port, token = got
        try:
            resp = await _agent_read(ip, port, token, name)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"sandbox agent unreachable: {e}") from None
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
            headers={
                "Content-Disposition": resp.headers.get(
                    "content-disposition", f'inline; filename="{name}"'
                )
            },
        )

    @guarded.post("/sandbox/reset")
    async def reset(body: ResetRequest):
        await pool().release(body.thread_id)
        return {"ok": True}

    app.include_router(open_)
    app.include_router(guarded)

    @app.get("/")
    async def root():
        return {"message": f"{settings.APP_NAME} orchestrator"}

    return app
