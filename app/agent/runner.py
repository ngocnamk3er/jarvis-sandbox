"""The in-pod bash runner.

One sandbox pod == one conversation, so there is **no jail here**. The pod is
the isolation boundary:

    * its own PID / network / mount / IPC / UTS namespace (k8s gives every pod
      these)
    * runs as an unprivileged uid, every Linux capability dropped,
      `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`
    * a NetworkPolicy that lets it reach the internet (pip / curl) but not any
      other pod or Service in the cluster, nor the node metadata IP
    * deleted the moment the conversation ends or goes idle — never reused

So this module just runs the command in `/workspace` and streams back
stdout / stderr / exit_code, and reads a file back out for `present_file`.
Compared to the old shared-container runner it drops `unshare`, `setpriv`,
`mount --bind`, the tmpfs masking and the per-thread uid — the pod replaces
all of it.
"""

import asyncio
import contextlib
import logging
import mimetypes
import os
import shutil
import signal
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 100_000  # stdout/stderr truncated past this, per stream
_MAX_READ_BYTES = 25 * 1024 * 1024  # `read_file` (present_file) size cap

# python:3.11-slim's /etc/mime.types is missing the OOXML office formats — add
# them so present_file / the chat download chip get a useful Content-Type.
for _ext, _mime in {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}.items():
    mimetypes.add_type(_mime, _ext)


def _workspace() -> Path:
    ws = Path(settings.WORKSPACE_DIR)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _truncate(b: bytes) -> str:
    text = b.decode("utf-8", errors="replace")
    if len(b) > _MAX_OUTPUT_BYTES:
        return text[:_MAX_OUTPUT_BYTES] + f"\n... [truncated, {len(b)} bytes total]"
    return text


async def exec_command(command: str, timeout_seconds: int) -> dict:
    """Run one bash `command` in /workspace and return
    {stdout, stderr, exit_code, timed_out}.
    """
    ws = _workspace()
    env = {
        "HOME": str(ws),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }

    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        command,
        cwd=str(ws),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # New session/process-group so a timeout can SIGKILL the whole tree.
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        return {
            "stdout": _truncate(stdout),
            "stderr": _truncate(stderr),
            "exit_code": proc.returncode,
            "timed_out": False,
        }
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout_seconds}s and was killed.",
            "exit_code": None,
            "timed_out": True,
        }


def read_file(name: str) -> tuple[bytes, str, str]:
    """Return (bytes, mime_type, basename) for a file in the workspace.

    `name` must be a relative path with no ".." — the same containment check
    as before (reject absolute + "..", `.resolve()` to follow symlinks,
    require the result inside the workspace), because this still runs as a
    normal process the kernel would happily let follow a symlink out.
    """
    ws = _workspace().resolve()
    raw = Path(name)
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("path must be a relative name inside the workspace")
    target = (ws / raw).resolve()
    if target != ws and ws not in target.parents:
        raise ValueError("path escapes the workspace")
    if not target.exists():
        raise FileNotFoundError(name)
    if target.is_dir():
        raise IsADirectoryError(name)
    size = target.stat().st_size
    if size > _MAX_READ_BYTES:
        raise ValueError(f"file is {size} bytes, over the {_MAX_READ_BYTES} limit")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return target.read_bytes(), mime, target.name


def reset() -> None:
    """Wipe everything in the workspace. Rarely needed now — the orchestrator
    deletes the whole pod on /reset — but kept so local `make dev` (one
    long-lived agent) still honours the backend's reset call.
    """
    ws = _workspace()
    with contextlib.suppress(FileNotFoundError):
        for child in ws.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    child.unlink()
