"""The whole sandbox: one shared container, but every conversation runs in
its own private view.

On disk: DATA_ROOT/<thread_id>/ is a conversation's "prefix" — the service
manages it; the agent never sees it. DATA_ROOT/<thread_id>/workspace/ is the
only thing the agent touches: each `exec_command` runs inside a fresh mount
namespace where that dir is bind-mounted onto /workspace, and the command
runs as a per-thread uid. So `cd /workspace/<other>` can't resolve, `pwd` is
always /workspace, and one conversation cannot read or write another's files
(previously it could — they were all siblings under a world-writable
/workspace, same uid)."""

import asyncio
import contextlib
import mimetypes
import os
import re
import shutil
import signal
import time
import zlib
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


def _safe_id(thread_id: str) -> str:
    safe = _SAFE_ID.sub("", thread_id or "")
    if not safe:
        raise ValueError("invalid thread_id")
    return safe


def _thread_root(thread_id: str) -> Path:
    """DATA_ROOT/<thread_id> — the conversation's prefix (service-only)."""
    return Path(settings.DATA_ROOT) / _safe_id(thread_id)


def _thread_workspace(thread_id: str) -> Path:
    """DATA_ROOT/<thread_id>/workspace — bind-mounted to /workspace per exec."""
    return _thread_root(thread_id) / "workspace"


def _thread_uid(thread_id: str) -> int:
    """A stable, distinct uid per conversation for setpriv."""
    return settings.UID_BASE + zlib.crc32(_safe_id(thread_id).encode()) % settings.UID_RANGE


def _prepare(thread_id: str) -> tuple[Path, Path, int]:
    """mkdir the thread's tree, chown it to the thread uid, touch the marker
    (marker lives at the prefix, outside the agent's /workspace view)."""
    root = _thread_root(thread_id)
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    uid = _thread_uid(thread_id)
    for p in (root, ws):
        with contextlib.suppress(OSError):
            os.chown(p, uid, uid)
    (root / _MARKER).touch()
    return root, ws, uid


def _touch(root: Path) -> None:
    with contextlib.suppress(OSError):
        (root / _MARKER).touch()


def _truncate(b: bytes) -> str:
    text = b.decode("utf-8", errors="replace")
    if len(b) > _MAX_OUTPUT_BYTES:
        return text[:_MAX_OUTPUT_BYTES] + f"\n... [truncated, {len(b)} bytes total]"
    return text


# The command runs: in a private mount namespace, with the thread's own
# workspace bind-mounted onto /workspace, dropped to the thread's uid.
# `mount` needs CAP_SYS_ADMIN so it happens before setpriv drops privileges.
_JAIL = (
    'mkdir -p /workspace && mount --bind "$SBX_WS" /workspace && cd /workspace && '
    'exec setpriv --reuid "$SBX_UID" --regid "$SBX_UID" --clear-groups '
    '     --inh-caps=-all bash -c "$SBX_CMD"'
)


async def exec_command(thread_id: str, command: str, timeout_seconds: int) -> dict:
    root, ws, uid = _prepare(thread_id)

    proc = await asyncio.create_subprocess_exec(
        "unshare", "--mount", "--propagation", "private", "--", "sh", "-c", _JAIL,
        cwd="/",
        env={
            "SBX_WS": str(ws),
            "SBX_UID": str(uid),
            "SBX_CMD": command,          # only ever reaches `bash -c "$SBX_CMD"`
            "HOME": "/workspace",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group so a timeout can kill the whole tree.
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
        _touch(root)


def read_file(thread_id: str, name: str) -> tuple[bytes, str, str]:
    """Returns (bytes, mime_type, basename). Raises FileNotFoundError /
    IsADirectoryError / ValueError.

    `name` must be a relative path with no `..` — it resolves inside this
    conversation's own workspace and nowhere else. The service runs as root
    (so the OS won't stop a traversal here); this check is the only thing
    that does."""
    ws = _thread_workspace(thread_id).resolve()
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


def reset(thread_id: str) -> None:
    """Wipe a conversation's whole tree (prefix + workspace)."""
    with contextlib.suppress(ValueError):
        shutil.rmtree(_thread_root(thread_id), ignore_errors=True)


async def gc_loop() -> None:
    root = Path(settings.DATA_ROOT)
    interval = 600
    while True:
        await asyncio.sleep(interval)
        cutoff = time.time() - settings.IDLE_GC_MINUTES * 60
        with contextlib.suppress(FileNotFoundError):
            for child in root.iterdir():          # DATA_ROOT/<thread_id>
                if not child.is_dir():
                    continue
                marker = child / _MARKER
                mtime = marker.stat().st_mtime if marker.exists() else child.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
