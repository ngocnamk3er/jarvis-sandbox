"""The whole sandbox — one shared container, but every conversation runs in
its own private view.

Big picture
-----------
This service is ONE Linux container (one pod). It does not spin up a container
per conversation. Instead, every time an agent runs a bash command, we wrap
that command in a throwaway **Linux namespace jail** so that, from inside, it
looks and behaves like the command has its own tiny machine:

    * /workspace  -> this conversation's own directory (and nothing else)
    * /data       -> empty (other conversations' dirs are invisible)
    * /tmp        -> a fresh private scratch dir, wiped when the command ends
    * /proc       -> only this command's own processes
    * uid         -> a per-conversation unprivileged user, no capabilities

The jail is built from four standard kernel features (see `_JAIL` below):

    unshare(2)      new mount + PID namespaces  -> a private "view"
    mount --bind    make dir A also appear at path B
    tmpfs           an empty in-RAM filesystem, mounted OVER a path to mask it
    setpriv(1)      drop to an unprivileged uid with zero capabilities

On disk the service (running as root) manages:

    DATA_ROOT/                       0711  root   (traversable, NOT listable)
    DATA_ROOT/<thread_id>/           0700  <uid>  the conversation "prefix"
    DATA_ROOT/<thread_id>/.last_used 0644  root   GC marker (agent never sees it)
    DATA_ROOT/<thread_id>/workspace/ 0700  <uid>  bind-mounted onto /workspace

Isolation is three layers deep, so one has to fail before the next matters:
    1. mount namespace  -> a sibling thread's path does not exist in the view
    2. uid + 0700 perms -> even given the real path, the kernel denies it
    3. no privileges    -> the jailed command can't rebuild namespaces/mounts
                           to climb out (caps dropped, setuid bits stripped
                           from the image)
"""

import asyncio
import contextlib
import logging
import mimetypes
import os
import re
import shutil
import signal
import time
import zlib
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 100_000  # stdout/stderr are truncated past this per stream
_MAX_READ_BYTES = 25 * 1024 * 1024  # `read_file` (present_file) size cap
_MARKER = ".last_used"  # touched every exec; GC deletes dirs whose marker is stale
# thread_id sanitiser: keep only chars that are safe in a path component. A
# stripped-to-empty id is rejected (never silently collapse two threads into
# one shared dir).
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

# python:3.11-slim's /etc/mime.types is missing the OOXML office formats — add
# them so present_file / the chat download chip get a useful Content-Type.
for _ext, _mime in {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}.items():
    mimetypes.add_type(_mime, _ext)


# ---------------------------------------------------------------------------
# paths + per-thread uid
# ---------------------------------------------------------------------------
def _safe_id(thread_id: str) -> str:
    safe = _SAFE_ID.sub("", thread_id or "")
    if not safe:
        raise ValueError("invalid thread_id")
    return safe


def _thread_root(thread_id: str) -> Path:
    """DATA_ROOT/<thread_id> — the conversation's "prefix". The service touches
    this (marker file, GC); the agent never sees it, because the jail bind-
    mounts one level deeper (`workspace/`) onto /workspace."""
    return Path(settings.DATA_ROOT) / _safe_id(thread_id)


def _thread_workspace(thread_id: str) -> Path:
    """DATA_ROOT/<thread_id>/workspace — this is what becomes /workspace inside
    the jail, and the only directory the agent can read or write."""
    return _thread_root(thread_id) / "workspace"


def _thread_uid(thread_id: str) -> int:
    """A stable, distinct Linux uid per conversation, used by `setpriv` to run
    the command as an unprivileged user.

    Derived from the thread_id so it's the same across calls without keeping a
    uid<->thread table. crc32 can collide (two thread_ids -> same uid) roughly
    once you have ~sqrt(UID_RANGE) live threads, but that's only a *second*
    line of defence: the mount namespace already makes a sibling's files
    unreachable regardless of uid. The service itself runs as root, so a
    collision never affects marker/GC/read_file bookkeeping.
    """
    return settings.UID_BASE + zlib.crc32(_safe_id(thread_id).encode()) % settings.UID_RANGE


def _prepare(thread_id: str) -> tuple[Path, Path, int]:
    """Make sure the conversation's tree exists with the right owner + perms,
    and bump its GC marker. Runs as root before every exec.

    Ordering matters: `chmod` BEFORE `chown`. While the dir is still root-owned,
    root can chmod it without CAP_FOWNER; after `chown` it belongs to the
    thread uid and chmod would need that capability.
    """
    root = _thread_root(thread_id)
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    uid = _thread_uid(thread_id)
    for p in (root, ws):
        with contextlib.suppress(OSError):
            os.chmod(p, 0o700)  # 0700: only this thread's uid. DATA_ROOT is
            #                     0711 (traversable) so this is the wall.
        with contextlib.suppress(OSError):
            os.chown(p, uid, uid)
    # Marker lives at the prefix (root-owned dir the agent can't see), so
    # `ls /workspace` never shows a stray dotfile.
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


# ---------------------------------------------------------------------------
# running a bash command  (the security-critical part)
# ---------------------------------------------------------------------------
#
# `unshare --mount --pid --fork -- sh -c "$_JAIL"` starts a shell that is
# already inside fresh mount + PID namespaces. It's still root at this point
# (unshare needs CAP_SYS_ADMIN, which the pod has), so it can mount things.
# The shell then, in order:
#
#   1. mount --bind "$SBX_WS" /workspace
#        "$SBX_WS" is DATA_ROOT/<thread_id>/workspace. A bind mount makes that
#        directory ALSO appear at /workspace. Because we're in a private mount
#        namespace, this rebinding is invisible to the rest of the pod and to
#        every other jailed command.
#
#   2. mount -t tmpfs -o mode=000 tmpfs "$SBX_DATA"
#        Mount an empty in-RAM filesystem OVER /data. Whatever is really at
#        /data (all the other threads' dirs) is now hidden behind an empty,
#        unreadable (mode 000) directory. `cat /data/<other>/...` -> no such
#        path. The real data is untouched on disk; it's just covered.
#
#   3. mount -t tmpfs /tmp   and   mount -t tmpfs /var/tmp, /dev/shm
#        Each command gets its own private scratch space. Two conversations
#        can't use /tmp as a shared drop box, and nothing survives the command.
#
#   4. mount -t proc proc /proc
#        With the new PID namespace, a fresh /proc shows only this command's
#        own processes — `ps`, /proc/<pid>/... can't see other conversations
#        or the service itself.
#
#   5. exec setpriv --reuid <uid> --regid <uid> --clear-groups --inh-caps=-all
#           bash -c "$SBX_CMD"
#        Permanently drop from root to the per-thread unprivileged uid and to
#        ZERO capabilities, then run the agent's command. From here it cannot
#        mount, unshare, chown, read another uid's 0700 files, or escalate —
#        the jail is sealed. `$SBX_CMD` is passed via the environment and only
#        ever reaches `bash -c`, never the wrapper, so it can't break out of
#        the `sh -c` string.
#
# `--propagation private` keeps every mount above from leaking back into the
# pod's real mount table. When the command exits, the namespaces are torn
# down and all of these mounts vanish — no cleanup needed.
_JAIL = (
    'mkdir -p /workspace && mount --bind "$SBX_WS" /workspace && '
    'mount -t tmpfs -o mode=000,size=1M tmpfs "$SBX_DATA" && '
    "mount -t tmpfs -o mode=1777,size=64m tmpfs /tmp && "
    "mount -t tmpfs -o mode=1777,size=16m tmpfs /var/tmp && "
    "mount -t tmpfs -o mode=1777,size=16m tmpfs /dev/shm && "
    "mount -t proc proc /proc && "
    "cd /workspace && "
    'exec setpriv --reuid "$SBX_UID" --regid "$SBX_UID" --clear-groups '
    '     --inh-caps=-all bash -c "$SBX_CMD"'
)


async def exec_command(thread_id: str, command: str, timeout_seconds: int) -> dict:
    """Run one bash `command` for a conversation and return
    {stdout, stderr, exit_code, timed_out}.

    The command runs jailed (see `_JAIL`), as an unprivileged per-thread uid,
    with `/workspace` = this conversation's directory and nothing else reachable.
    """
    root, ws, uid = _prepare(thread_id)

    if settings.UNSAFE_NO_JAIL:
        # LOCAL DEV ESCAPE HATCH — no isolation at all, just cwd. See the
        # UNSAFE_NO_JAIL note in config.py. Never enable this where more than
        # one person's conversations share the process.
        logger.warning("UNSAFE_NO_JAIL: running bash with NO sandbox isolation")
        argv = ["bash", "-c", command]
        cwd = str(ws)
        env = {
            "HOME": str(ws),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    else:
        argv = [
            # argv, NOT a shell string — `command` never touches this layer.
            "unshare",
            "--mount",  # new mount namespace: our binds/tmpfs are private
            "--pid",  # new PID namespace: /proc only shows our own processes
            "--fork",  # unshare forks so the child (our sh) becomes PID 1 of the ns
            "--propagation",
            "private",  # don't leak mounts back to the pod
            "--",
            "sh",
            "-c",
            _JAIL,
        ]
        cwd = "/"
        env = {
            # Only these vars exist inside the command — no INTERNAL_API_KEY,
            # no other jarvis secrets.
            "SBX_WS": str(ws),  # -> bound onto /workspace
            "SBX_DATA": settings.DATA_ROOT,  # -> masked with an empty tmpfs
            "SBX_UID": str(uid),  # -> setpriv target
            "SBX_CMD": command,  # -> only reaches `bash -c "$SBX_CMD"`
            "HOME": "/workspace",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # New session/process-group so a timeout can SIGKILL the whole tree
        # (the shell, its children, and everything they spawned) in one call.
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
        # Kill the process group. `proc.pid` is the `unshare` process; killing
        # its group takes down unshare + the PID-1-of-namespace shell, and the
        # kernel then reaps every remaining process in that PID namespace.
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


# ---------------------------------------------------------------------------
# reading a file back out  (powers present_file / the chat download chip)
# ---------------------------------------------------------------------------
def read_file(thread_id: str, name: str) -> tuple[bytes, str, str]:
    """Return (bytes, mime_type, basename) for a file in the conversation's
    workspace. Raises FileNotFoundError / IsADirectoryError / ValueError.

    This runs in the SERVICE process (root, not jailed), so the kernel will
    happily follow `name` anywhere. The containment check below is the ONLY
    thing stopping `name = "../<other_thread>/secret"` or a symlink that points
    out of the workspace:

      * reject absolute paths and any ".." component up front
      * `.resolve()` the target (this also follows symlinks)
      * require the resolved path to be the workspace dir itself or inside it
    """
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


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
def reset(thread_id: str) -> None:
    """Delete a conversation's whole tree (prefix + workspace). Called by
    jarvis-backend on /chat/stop and when a conversation is deleted."""
    with contextlib.suppress(ValueError):
        shutil.rmtree(_thread_root(thread_id), ignore_errors=True)


async def gc_loop() -> None:
    """Every 10 minutes, delete any conversation dir whose `.last_used` marker
    (or the dir itself) hasn't been touched for IDLE_GC_MINUTES. Keeps the
    emptyDir from filling up with abandoned workspaces."""
    root = Path(settings.DATA_ROOT)
    interval = 600
    while True:
        await asyncio.sleep(interval)
        cutoff = time.time() - settings.IDLE_GC_MINUTES * 60
        with contextlib.suppress(FileNotFoundError):
            for child in root.iterdir():  # DATA_ROOT/<thread_id>
                if not child.is_dir():
                    continue
                marker = child / _MARKER
                mtime = marker.stat().st_mtime if marker.exists() else child.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
