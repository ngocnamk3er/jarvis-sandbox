"""One-pod-per-conversation sandboxes, created on demand.

The first time a conversation runs bash, `claim()` gives it an agent pod:
normally by creating one right then (waiting ~3-5s for Ready), or — if
`POOL_SIZE > 0` — by taking a pre-warmed one. The pod is relabelled `claimed`
and `thread_id -> pod` is recorded. `release()` (on /reset) deletes it.

A pod is deleted by the GC sweep when EITHER it has been idle for
`IDLE_GC_MINUTES` (no bash call) OR it has simply lived `SANDBOX_TTL_MINUTES`
(a hard per-pod lifetime). Total live pods never exceed `MAX_SANDBOXES`; past
that, `claim()` raises `CapacityError` and the caller gets a 503.

The `thread_id -> pod` mapping is authoritative in the pod's own labels; the
in-memory dict is a cache rebuilt from labels on startup, so an orchestrator
restart re-adopts the running sandboxes rather than orphaning them. The
per-pod agent token is likewise recovered from the pod's (literal) env.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time

from app.core.config import settings
from app.orchestrator import podspec
from app.orchestrator.k8s import K8sClient, PodView

logger = logging.getLogger(__name__)

_LABEL_SELECTOR = f"{podspec.LABEL_APP}={podspec.APP_VALUE}"
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]")


class CapacityError(RuntimeError):
    """All MAX_SANDBOXES slots are in use."""


class ClaimTimeout(RuntimeError):
    """An on-demand pod did not become Ready inside CLAIM_TIMEOUT_SECONDS."""


def safe_thread(thread_id: str) -> str:
    """thread_id -> a valid k8s label value ([A-Za-z0-9_.-], <=63). A
    stripped-to-empty id is rejected (never collapse two threads into one)."""
    s = _SAFE_LABEL.sub("-", thread_id or "").strip("-.")[:63].strip("-.")
    if not s:
        raise ValueError("invalid thread_id")
    return s


class SandboxPool:
    def __init__(self, k8s: K8sClient):
        self.k8s = k8s
        self._image: str | None = settings.SANDBOX_IMAGE or None
        self._lock = asyncio.Lock()  # serialises all pool mutations
        self._threads: dict[str, str] = {}  # safe thread_id -> pod name
        self._tokens: dict[str, str] = {}  # pod name -> agent token
        self._last_used: dict[str, float] = {}  # safe thread_id -> unix seconds

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        if not self._image:
            self._image = await self._discover_own_image()
            logger.info("agent image: %s", self._image)
        await self._reconcile()
        await self.ensure_pool()

    async def _discover_own_image(self) -> str:
        name = os.environ.get("HOSTNAME", "")
        pod = await self.k8s.read_pod(name) if name else None
        if not pod or not pod.image:
            raise RuntimeError("cannot determine agent image: set SANDBOX_IMAGE or run in-cluster")
        return pod.image

    async def _reconcile(self) -> None:
        pods = await self.k8s.list_pods(_LABEL_SELECTOR)
        threads: dict[str, str] = {}
        tokens: dict[str, str] = {}
        for p in pods:
            tok = p.env.get("AGENT_TOKEN")
            if tok:
                tokens[p.name] = tok
            if p.labels.get(podspec.LABEL_STATE) == podspec.STATE_CLAIMED:
                tid = p.labels.get(podspec.LABEL_THREAD)
                if tid:
                    threads[tid] = p.name
        self._threads, self._tokens = threads, tokens
        logger.info("reconciled: %d claimed, %d total pods", len(threads), len(pods))

    # ------------------------------------------------------------- pool top-up
    async def ensure_pool(self) -> None:
        async with self._lock:
            await self._ensure_pool_locked()

    async def _ensure_pool_locked(self) -> None:
        pods = await self.k8s.list_pods(_LABEL_SELECTOR)
        total = len(pods)
        warm = sum(1 for p in pods if p.labels.get(podspec.LABEL_STATE) == podspec.STATE_WARM)
        want = min(settings.POOL_SIZE - warm, settings.MAX_SANDBOXES - total)
        for _ in range(max(0, want)):
            await self._spawn_pod_locked()

    async def _spawn_pod_locked(self) -> PodView:
        """Create a fresh agent pod (labelled `warm` — `claim()` relabels it
        `claimed`, so a crash between the two just leaves a reusable warm pod)."""
        token = secrets.token_urlsafe(32)
        pod = await self.k8s.create_pod(podspec.build(self._image, token))
        self._tokens[pod.name] = token
        logger.info("agent pod %s created", pod.name)
        return pod

    # --------------------------------------------------------------- claim
    async def claim(self, thread_id: str) -> tuple[str, int, str]:
        """Return (pod_ip, agent_port, token) for this conversation, creating
        or reusing a pod as needed."""
        tid = safe_thread(thread_id)
        async with self._lock:
            existing = await self._live_pod_for_locked(tid)
            if existing is not None:
                self._last_used[tid] = time.time()
                return existing.pod_ip, settings.AGENT_PORT, self._tokens.get(existing.name, "")

            pod = await self._take_pod_locked()
            now = str(int(time.time()))
            await self.k8s.patch_pod_metadata(
                pod.name,
                labels={
                    podspec.LABEL_STATE: podspec.STATE_CLAIMED,
                    podspec.LABEL_THREAD: tid,
                },
                annotations={podspec.ANNO_LAST_USED: now, podspec.ANNO_CLAIMED_AT: now},
            )
            self._threads[tid] = pod.name
            self._last_used[tid] = time.time()
            ip = pod.pod_ip
            if not ip:
                fresh = await self.k8s.read_pod(pod.name)
                ip = fresh.pod_ip if fresh else None
            token = self._tokens.get(pod.name, "")

        # refill happens after releasing the lock
        asyncio.create_task(self._ensure_pool_bg())
        if not ip:
            raise ClaimTimeout(f"sandbox {pod.name} has no IP yet")
        return ip, settings.AGENT_PORT, token

    async def _live_pod_for_locked(self, tid: str) -> PodView | None:
        name = self._threads.get(tid)
        if not name:
            return None
        pod = await self.k8s.read_pod(name)
        if pod and pod.ready and pod.pod_ip and pod.phase == "Running":
            return pod
        # stale / gone / not ready — drop it and let the caller take a new one
        self._threads.pop(tid, None)
        return None

    async def _take_pod_locked(self) -> PodView:
        # Runs under self._lock. With POOL_SIZE=0 (the default) every claim
        # falls through to create-and-wait, holding the lock for up to
        # CLAIM_TIMEOUT_SECONDS — that's what caps total pods at MAX_SANDBOXES
        # without a race. Raise POOL_SIZE if that ~3-5s serialised wait on a
        # conversation's first bash call matters.
        pods = await self.k8s.list_pods(_LABEL_SELECTOR)
        warm_ready = [
            p
            for p in pods
            if p.labels.get(podspec.LABEL_STATE) == podspec.STATE_WARM and p.ready and p.pod_ip
        ]
        if warm_ready:
            return warm_ready[0]
        if len(pods) >= settings.MAX_SANDBOXES:
            raise CapacityError(
                f"all {settings.MAX_SANDBOXES} sandbox slots are in use — retry shortly"
            )
        pod = await self._spawn_pod_locked()
        return await self._wait_ready_locked(pod.name)

    async def _wait_ready_locked(self, name: str) -> PodView:
        deadline = time.time() + settings.CLAIM_TIMEOUT_SECONDS
        while time.time() < deadline:
            pod = await self.k8s.read_pod(name)
            if pod is None:
                break
            if pod.ready and pod.pod_ip and pod.phase == "Running":
                return pod
            if pod.phase in ("Failed", "Succeeded"):
                break
            await asyncio.sleep(1)
        await self.k8s.delete_pod(name)
        self._tokens.pop(name, None)
        raise ClaimTimeout(f"sandbox {name} not Ready in {settings.CLAIM_TIMEOUT_SECONDS}s")

    # --------------------------------------------------------------- read/touch
    async def get(self, thread_id: str) -> tuple[str, int, str] | None:
        tid = safe_thread(thread_id)
        name = self._threads.get(tid)
        if not name:
            return None
        pod = await self.k8s.read_pod(name)
        if not pod or not pod.ready or not pod.pod_ip:
            return None
        return pod.pod_ip, settings.AGENT_PORT, self._tokens.get(name, "")

    def touch(self, thread_id: str) -> None:
        try:
            self._last_used[safe_thread(thread_id)] = time.time()
        except ValueError:
            pass

    # --------------------------------------------------------------- release
    async def release(self, thread_id: str) -> None:
        try:
            tid = safe_thread(thread_id)
        except ValueError:
            return
        async with self._lock:
            name = self._threads.pop(tid, None)
            self._last_used.pop(tid, None)
        if name:
            await self.k8s.delete_pod(name)
            self._tokens.pop(name, None)
            logger.info("released sandbox %s (thread %s)", name, tid)
        asyncio.create_task(self._ensure_pool_bg())

    async def _delete_claimed_locked(self, name: str, tid: str | None) -> None:
        """Delete one claimed pod and forget its thread. Caller holds the lock."""
        await self.k8s.delete_pod(name)
        self._tokens.pop(name, None)
        if tid:
            self._threads.pop(tid, None)
            self._last_used.pop(tid, None)

    # --------------------------------------------------------------- GC
    async def gc_once(self) -> None:
        idle_cutoff = time.time() - settings.IDLE_GC_MINUTES * 60
        ttl_seconds = settings.SANDBOX_TTL_MINUTES * 60
        async with self._lock:
            pods = await self.k8s.list_pods(_LABEL_SELECTOR)

            # drop cache entries whose pod vanished out from under us (node
            # eviction, a manual kubectl delete, a lost race)
            live = {p.name for p in pods}
            for tid, name in list(self._threads.items()):
                if name not in live:
                    self._threads.pop(tid, None)
                    self._last_used.pop(tid, None)
                    self._tokens.pop(name, None)

            for p in pods:
                state = p.labels.get(podspec.LABEL_STATE)
                if state == podspec.STATE_WARM:
                    # a warm pod that never came up, or one that has aged out
                    if (p.age_seconds() > 300 and not p.ready) or p.age_seconds() > ttl_seconds:
                        logger.info(
                            "GC warm pod %s (age %ds, ready=%s)", p.name, p.age_seconds(), p.ready
                        )
                        await self.k8s.delete_pod(p.name)
                        self._tokens.pop(p.name, None)
                    continue

                tid = p.labels.get(podspec.LABEL_THREAD)

                # hard per-pod lifetime cap
                if p.age_seconds() > ttl_seconds:
                    logger.info(
                        "GC sandbox %s (thread %s) — TTL %dm reached",
                        p.name,
                        tid,
                        settings.SANDBOX_TTL_MINUTES,
                    )
                    await self._delete_claimed_locked(p.name, tid)
                    continue

                last = self._last_used.get(tid or "")
                if last is None:
                    anno = p.annotations.get(podspec.ANNO_LAST_USED)
                    last = float(anno) if anno else p.created_ts
                if last < idle_cutoff:
                    logger.info("GC idle sandbox %s (thread %s)", p.name, tid)
                    await self._delete_claimed_locked(p.name, tid)
                elif tid and tid in self._last_used:
                    # flush the in-memory clock onto the pod so a restart
                    # doesn't reset the idle timer
                    await self.k8s.patch_pod_metadata(
                        p.name,
                        annotations={podspec.ANNO_LAST_USED: str(int(self._last_used[tid]))},
                    )
            await self._ensure_pool_locked()

    # --------------------------------------------------------------- loops
    async def reconcile_loop(self) -> None:
        if settings.POOL_SIZE <= 0:
            return  # pure on-demand — nothing to keep warm
        while True:
            await asyncio.sleep(settings.RECONCILE_INTERVAL_SECONDS)
            try:
                await self.ensure_pool()
            except Exception:
                logger.exception("ensure_pool failed")

    async def gc_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.GC_INTERVAL_SECONDS)
            try:
                await self.gc_once()
            except Exception:
                logger.exception("gc_once failed")

    async def _ensure_pool_bg(self) -> None:
        if settings.POOL_SIZE <= 0:
            return
        try:
            await self.ensure_pool()
        except Exception:
            logger.exception("background refill failed")

    # --------------------------------------------------------------- introspection
    def snapshot(self) -> dict:
        return {
            "image": self._image,
            "threads": dict(self._threads),
            "pool_size": settings.POOL_SIZE,
            "max_sandboxes": settings.MAX_SANDBOXES,
            "idle_gc_minutes": settings.IDLE_GC_MINUTES,
            "ttl_minutes": settings.SANDBOX_TTL_MINUTES,
        }
