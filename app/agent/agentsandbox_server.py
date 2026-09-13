"""Alternate entrypoint: exposes `app.agent.runner`'s existing bash-exec
logic through kubernetes-sigs/agent-sandbox's own runtime contract (GET /
health check, POST /execute) instead of jarvis-sandbox's own
`/api/v1/sandbox/*` protocol.

Not wired into `app.main`'s SANDBOX_ROLE dispatch — this is a standalone
image/entrypoint for evaluating agent-sandbox as a possible backend, run
via Dockerfile.agentsandbox, side by side with the real jarvis-sandbox
deployment (different image tag, different SandboxTemplate).

Request/response shape here matches the *installed* `k8s-agent-sandbox`
pip package's actual wire format, not the docs site (which describes an
older/different shape — `{"command": {"content": ..., "env": ...}}` in,
`exitCode` out — verified by reading
`k8s_agent_sandbox/commands/command_executor.py` and `models.py` directly
from the installed package: `CommandExecutor.run()` sends a plain
`{"command": "<string>"}` and parses the response as
`ExecutionResult(stdout, stderr, exit_code)` — snake_case, no `env` field
in the non-sandboxd HTTP path at all).

`/upload` and `/download/<path>` mirror the SDK's `Filesystem.write()` /
`.read()` for the same non-sandboxd path (`files/filesystem.py`): a
multipart POST with a `file` field (filename = destination path) in, a
plain `GET download/<url-encoded path>` out. The SDK does its own path
sanitizing client-side before sending; `runner._resolve_in_workspace()`
re-does the same class of check server-side rather than trusting that.
"""

import asyncio

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from app.agent import runner

app = FastAPI()


class ExecuteRequest(BaseModel):
    command: str


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Sandbox Runtime is active."}


@app.post("/execute")
async def execute_command(req: ExecuteRequest):
    result = await runner.exec_command(req.command, timeout_seconds=300)
    return {
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        # None on timeout — coerce to a real int since ExecutionResult.exit_code is int.
        "exit_code": result["exit_code"] if result["exit_code"] is not None else -1,
    }


@app.post("/upload")
async def upload_file(request: Request):
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(status_code=422, detail="missing 'file' field")
    content = await upload.read()
    try:
        await asyncio.to_thread(runner.write_file, upload.filename, content)
    except (ValueError, IsADirectoryError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {"status": "ok"}


@app.get("/download/{path:path}")
async def download_file(path: str):
    # Starlette already percent-decodes the raw URL once before routing, so
    # `path` here is the plain filename — no second unquote() needed (and
    # doing one would double-decode a name containing a literal `%`).
    try:
        content, mime, _basename = await asyncio.to_thread(runner.read_file, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"not found: {path}") from None
    except IsADirectoryError:
        raise HTTPException(status_code=400, detail=f"{path} is a directory") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return Response(content=content, media_type=mime)
