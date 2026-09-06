"""The whole sandbox: one shared container, a working directory per
conversation under WORKSPACE_ROOT/{thread_id}. No isolation beyond the pod
itself — every conversation shares CPU/RAM/disk (by design)."""

import asyncio
import contextlib
import mimetypes
import os
import re
import shutil
import signal
import time
from pathlib import Path

from app.core.config import settings

_MAX_OUTPUT_BYTES = 100_000  # per stream, truncated past this
_MAX_READ_BYTES = 25 * 1024 * 1024  # present_file cap
_MARKER = ".last_used"
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

# python:3.11-slim's mime.types is missing the OOXML office formats — register
# them so present_file / the chat download chip get a useful Content-Type.
for _ext, _mime in {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}.items():
    mimetypes.add_type(_mime, _ext)


def _thread_dir(thread_id: str) -> Path:
    safe = _SAFE_ID.sub("", thread_id) or "default"
    d = Path(settings.WORKSPACE_ROOT) / safe
    return d


def _touch(d: Path) -> None:
    (d / _MARKER).touch()


def _truncate(b: bytes) -> str:
    text = b.decode("utf-8", errors="replace")
    if len(b) > _MAX_OUTPUT_BYTES:
        return text[:_MAX_OUTPUT_BYTES] + f"\n... [truncated, {len(b)} bytes total]"
    return text


async def exec_command(thread_id: str, command: str, timeout_seconds: int) -> dict:
    d = _thread_dir(thread_id)
    d.mkdir(parents=True, exist_ok=True)
    _touch(d)

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(d),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group so a timeout can kill the whole tree, not just the shell.
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
    finally:
        _touch(d)


def read_file(thread_id: str, name: str) -> tuple[bytes, str, str]:
    """Returns (bytes, mime_type, basename). Raises FileNotFoundError /
    IsADirectoryError / ValueError(path escape) / ValueError(too large).

    A relative `name` resolves against the conversation's own dir; an absolute
    path is taken as-is. Either way it must land under the shared WORKSPACE_ROOT
    — conversations already share this pod's disk (by design), and the agent
    sometimes writes via `cd /workspace`, one level above its own dir."""
    root = Path(settings.WORKSPACE_ROOT).resolve()
    d = _thread_dir(thread_id).resolve()
    raw = Path(name)
    target = (raw if raw.is_absolute() else d / raw).resolve()
    if not (target == root or root in target.parents):
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


def reset(thread_id: str) -> None:
    shutil.rmtree(_thread_dir(thread_id), ignore_errors=True)


async def gc_loop() -> None:
    root = Path(settings.WORKSPACE_ROOT)
    interval = 600
    while True:
        await asyncio.sleep(interval)
        cutoff = time.time() - settings.IDLE_GC_MINUTES * 60
        with contextlib.suppress(FileNotFoundError):
            for child in root.iterdir():
                if child.is_dir():
                    marker = child / _MARKER
                    mtime = marker.stat().st_mtime if marker.exists() else child.stat().st_mtime
                    if mtime < cutoff:
                        shutil.rmtree(child, ignore_errors=True)
                # loose files at the root — an agent that ran `cd /workspace`
                # instead of staying in its own dir; drop them once stale too.
                elif child.stat().st_mtime < cutoff:
                    with contextlib.suppress(OSError):
                        child.unlink()
