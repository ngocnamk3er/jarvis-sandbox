"""HTTP surface for the sandbox: /sandbox/exec, /read, /reset.

Every route here is behind `verify_internal_key` — the service is only ever
called by jarvis-backend over the in-cluster network, never from the browser.
All the real logic (and all the isolation) lives in `app.services.runner`.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.api.deps import verify_internal_key
from app.core.config import settings
from app.services import runner

router = APIRouter(dependencies=[Depends(verify_internal_key)])


class ExecRequest(BaseModel):
    thread_id: str  # the conversation — picks which workspace + uid the command runs under
    command: str  # raw bash; runs jailed (see runner._JAIL), never eval'd by the wrapper
    timeout_seconds: int | None = None  # falls back to COMMAND_TIMEOUT_SECONDS


class ResetRequest(BaseModel):
    thread_id: str


@router.post("/exec")
async def exec_command(body: ExecRequest):
    """Run one bash command for a conversation. Returns
    {stdout, stderr, exit_code, timed_out}. Backs the agent's `bash` tool."""
    timeout = body.timeout_seconds or settings.COMMAND_TIMEOUT_SECONDS
    return await runner.exec_command(body.thread_id, body.command, timeout)


@router.get("/read")
async def read_file(thread_id: str, name: str):
    """Raw bytes of a file in the conversation's workspace — jarvis-backend
    proxies this for the `present_file` tool / the chat download chip.

    `name` must be a relative path with no ".." (runner.read_file enforces it,
    since this call runs as root and the kernel wouldn't stop a traversal).
    `runner.read_file` is sync + does blocking disk IO, so it's pushed to a
    worker thread.
    """
    try:
        content, mime, basename = await asyncio.to_thread(runner.read_file, thread_id, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"not found: {name}") from None
    except IsADirectoryError:
        raise HTTPException(status_code=400, detail=f"{name} is a directory") from None
    except ValueError as e:  # path escape / too large
        raise HTTPException(status_code=400, detail=str(e)) from None
    return Response(
        content=content,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{basename}"'},
    )


@router.post("/reset")
async def reset(body: ResetRequest):
    """Wipe a conversation's workspace. Called on /chat/stop and conversation
    delete. Idempotent — a missing thread is a no-op."""
    await asyncio.to_thread(runner.reset, body.thread_id)
    return {"ok": True}
