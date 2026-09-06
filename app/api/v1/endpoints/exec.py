import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.api.deps import verify_internal_key
from app.core.config import settings
from app.services import runner

router = APIRouter(dependencies=[Depends(verify_internal_key)])


class ExecRequest(BaseModel):
    thread_id: str
    command: str
    timeout_seconds: int | None = None


class ResetRequest(BaseModel):
    thread_id: str


@router.post("/exec")
async def exec_command(body: ExecRequest):
    timeout = body.timeout_seconds or settings.COMMAND_TIMEOUT_SECONDS
    return await runner.exec_command(body.thread_id, body.command, timeout)


@router.get("/read")
async def read_file(thread_id: str, name: str):
    """Raw bytes of a file in the conversation's workspace — jarvis-backend
    proxies this for the present_file tool / the chat download chip."""
    try:
        content, mime, basename = await asyncio.to_thread(runner.read_file, thread_id, name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"not found: {name}")
    except IsADirectoryError:
        raise HTTPException(status_code=400, detail=f"{name} is a directory")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(
        content=content,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{basename}"'},
    )


@router.post("/reset")
async def reset(body: ResetRequest):
    await asyncio.to_thread(runner.reset, body.thread_id)
    return {"ok": True}
