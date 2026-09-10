import time

import pytest
from app.core.config import settings
from app.orchestrator import podspec
from app.orchestrator.pool import CapacityError, SandboxPool, safe_thread


def test_safe_thread():
    assert safe_thread("abc-123") == "abc-123"
    assert safe_thread("a/b:c d") == "a-b-c-d"
    assert safe_thread("x" * 200) == "x" * 63
    with pytest.raises(ValueError):
        safe_thread("")
    with pytest.raises(ValueError):
        safe_thread("///")


async def test_start_fills_warm_pool(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 2)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 5)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    warm = [p for p in fake_k8s._pods.values() if p.labels[podspec.LABEL_STATE] == "warm"]
    assert len(warm) == 2


async def test_start_reconciles_existing_claimed(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 1)
    fake_k8s.seed_claimed("thread-a", token="secret-a")
    pool = SandboxPool(fake_k8s)
    await pool.start()
    assert pool._threads["thread-a"].startswith("sbx-")
    got = await pool.get("thread-a")
    assert got is not None
    ip, port, token = got
    assert token == "secret-a"


async def test_claim_uses_warm_pod_then_refills(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 1)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 5)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    assert sum(p.labels[podspec.LABEL_STATE] == "warm" for p in fake_k8s._pods.values()) == 1

    ip, port, token = await pool.claim("conv-1")
    assert ip and port == settings.AGENT_PORT and token

    states = [p.labels[podspec.LABEL_STATE] for p in fake_k8s._pods.values()]
    assert states.count("claimed") == 1
    # background refill (fired via asyncio.create_task) restores the warm pod
    for _ in range(20):
        if (
            sum(
                s == "warm"
                for s in (p.labels[podspec.LABEL_STATE] for p in fake_k8s._pods.values())
            )
            == 1
        ):
            break
        await _tick()
    assert sum(p.labels[podspec.LABEL_STATE] == "warm" for p in fake_k8s._pods.values()) == 1


async def test_claim_is_idempotent_per_thread(fake_k8s):
    pool = SandboxPool(fake_k8s)
    await pool.start()
    a = await pool.claim("conv-x")
    b = await pool.claim("conv-x")
    assert a == b
    assert len(pool._threads) == 1


async def test_claim_on_demand_when_no_warm(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 3)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    assert not fake_k8s._pods
    ip, _, _ = await pool.claim("conv-1")
    assert ip
    assert len(fake_k8s._pods) == 1


async def test_capacity_cap(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 2)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    await pool.claim("c1")
    await pool.claim("c2")
    with pytest.raises(CapacityError):
        await pool.claim("c3")


async def test_release_deletes_pod(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 3)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    await pool.claim("c1")
    name = pool._threads["c1"]
    await pool.release("c1")
    assert "c1" not in pool._threads
    assert name in fake_k8s.deleted


async def test_gc_removes_idle_sandbox(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 3)
    monkeypatch.setattr(settings, "IDLE_GC_MINUTES", 30)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    await pool.claim("c1")
    name = pool._threads["c1"]
    # make it look untouched for 40 minutes
    pool._last_used["c1"] = time.time() - 40 * 60
    fake_k8s._pods[name].annotations[podspec.ANNO_LAST_USED] = str(int(time.time() - 40 * 60))
    await pool.gc_once()
    assert name in fake_k8s.deleted
    assert "c1" not in pool._threads


async def test_gc_keeps_active_sandbox(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 3)
    monkeypatch.setattr(settings, "IDLE_GC_MINUTES", 30)
    monkeypatch.setattr(settings, "SANDBOX_TTL_MINUTES", 180)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    await pool.claim("c1")
    name = pool._threads["c1"]
    pool.touch("c1")
    await pool.gc_once()
    assert name not in fake_k8s.deleted


async def test_gc_enforces_ttl_even_when_active(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 3)
    monkeypatch.setattr(settings, "IDLE_GC_MINUTES", 30)
    monkeypatch.setattr(settings, "SANDBOX_TTL_MINUTES", 60)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    await pool.claim("c1")
    name = pool._threads["c1"]
    pool.touch("c1")  # still active...
    fake_k8s._pods[name].created_ts = time.time() - 61 * 60  # ...but 61 min old
    await pool.gc_once()
    assert name in fake_k8s.deleted
    assert "c1" not in pool._threads


async def test_gc_prunes_cache_for_vanished_pod(fake_k8s, monkeypatch):
    monkeypatch.setattr(settings, "POOL_SIZE", 0)
    monkeypatch.setattr(settings, "MAX_SANDBOXES", 3)
    pool = SandboxPool(fake_k8s)
    await pool.start()
    await pool.claim("c1")
    name = pool._threads["c1"]
    # pod disappears without going through release()
    del fake_k8s._pods[name]
    await pool.gc_once()
    assert "c1" not in pool._threads
    assert name not in pool._tokens


async def _tick(n: int = 1):
    import asyncio

    for _ in range(n):
        await asyncio.sleep(0)
