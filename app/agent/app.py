"""HTTP surface of a sandbox pod (the "agent" role).

Only the orchestrator ever calls this, over the in-cluster network, holding
the per-pod `AGENT_TOKEN`. The route shapes mirror the old service so the
orchestrator can proxy straight through:

    POST /api/v1/sandbox/exec   {command, timeout_seconds?}  -> {stdout,...}
    GET  /api/v1/sandbox/read   ?name=...                     -> raw bytes
    POST /api/v1/sandbox/reset                                -> {ok: true}
    GET  /api/v1/health
"""

import asyncio

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from app.agent import runner
from app.core.config import settings


async def verify_agent_token(x_agent_token: str = Header(default="")) -> None:
    """Per-pod bearer token. Empty AGENT_TOKEN (local dev) disables the check."""
    if settings.AGENT_TOKEN and x_agent_token != settings.AGENT_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing agent token")


class ExecRequest(BaseModel):
    command: str
    timeout_seconds: int | None = None
    # Accepted and ignored — the whole pod belongs to one conversation. Kept
    # so a plain backend call (local dev, agent role) still works unchanged.
    thread_id: str | None = None


class ResetRequest(BaseModel):
    thread_id: str | None = None


def build_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.APP_NAME} (agent)",
        version=settings.APP_VERSION,
        docs_url=None,
        redoc_url=None,
    )
    guarded = APIRouter(
        prefix=settings.API_PREFIX,
        dependencies=[Depends(verify_agent_token)],
    )
    open_ = APIRouter(prefix=settings.API_PREFIX)

    @open_.get("/health")
    async def health():
        return {"status": "ok", "role": "agent", "version": settings.APP_VERSION}

    @guarded.post("/sandbox/exec")
    async def exec_command(body: ExecRequest):
        timeout = body.timeout_seconds or settings.COMMAND_TIMEOUT_SECONDS
        return await runner.exec_command(body.command, timeout)

    @guarded.get("/sandbox/read")
    async def read_file(name: str, thread_id: str | None = None):
        try:
            content, mime, basename = await asyncio.to_thread(runner.read_file, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"not found: {name}") from None
        except IsADirectoryError:
            raise HTTPException(status_code=400, detail=f"{name} is a directory") from None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return Response(
            content=content,
            media_type=mime,
            headers={"Content-Disposition": f'inline; filename="{basename}"'},
        )

    @guarded.post("/sandbox/reset")
    async def reset(body: ResetRequest):
        await asyncio.to_thread(runner.reset)
        return {"ok": True}

    app.include_router(open_)
    app.include_router(guarded)

    @app.get("/")
    async def root():
        return {"message": f"{settings.APP_NAME} agent"}

    return app
