"""Shared fixtures. A `FakeK8s` stands in for `app.orchestrator.k8s.K8sClient`
— same method signatures, returns the same `PodView` dataclass — so the pool
logic can be exercised without a cluster.
"""

import itertools
import os
import time

import pytest

os.environ.setdefault("SANDBOX_ROLE", "orchestrator")
os.environ.setdefault("SANDBOX_IMAGE", "jarvis-sandbox:test")
os.environ.setdefault("POD_NAMESPACE", "jarvis-test")

from app.orchestrator import podspec  # noqa: E402
from app.orchestrator.k8s import PodView  # noqa: E402


class FakeK8s:
    """In-memory pod store. Pods become Ready with an IP as soon as they are
    created (no scheduler to wait for)."""

    def __init__(self, ready_on_create: bool = True):
        self.namespace = "jarvis-test"
        self._pods: dict[str, PodView] = {}
        self._ids = itertools.count(1)
        self.ready_on_create = ready_on_create
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.patches: list[tuple[str, dict, dict]] = []

    async def start(self):  # pragma: no cover - trivial
        pass

    async def close(self):  # pragma: no cover - trivial
        pass

    async def list_pods(self, label_selector: str) -> list[PodView]:
        # only supports the single "app=..." selector the pool uses
        return list(self._pods.values())

    async def read_pod(self, name: str) -> PodView | None:
        return self._pods.get(name)

    async def create_pod(self, manifest: dict) -> PodView:
        name = f"{manifest['metadata']['generateName']}{next(self._ids):04d}"
        env = {}
        for e in manifest["spec"]["containers"][0]["env"]:
            if "value" in e:
                env[e["name"]] = e["value"]
        pod = PodView(
            name=name,
            labels=dict(manifest["metadata"]["labels"]),
            annotations={},
            phase="Running" if self.ready_on_create else "Pending",
            pod_ip="10.42.0." + str(len(self._pods) + 2) if self.ready_on_create else None,
            ready=self.ready_on_create,
            created_ts=time.time(),
            env=env,
            image=manifest["spec"]["containers"][0]["image"],
        )
        self._pods[name] = pod
        self.created.append(name)
        return pod

    async def delete_pod(self, name: str) -> None:
        self._pods.pop(name, None)
        self.deleted.append(name)

    async def patch_pod_metadata(self, name, labels=None, annotations=None) -> None:
        pod = self._pods.get(name)
        if pod is None:
            return
        if labels:
            pod.labels.update(labels)
        if annotations:
            pod.annotations.update(annotations)
        self.patches.append((name, labels or {}, annotations or {}))

    # ---- test helpers ---------------------------------------------------
    def seed_claimed(self, thread: str, token: str = "tok") -> PodView:
        name = f"sbx-{next(self._ids):04d}"
        pod = PodView(
            name=name,
            labels={
                podspec.LABEL_APP: podspec.APP_VALUE,
                podspec.LABEL_STATE: podspec.STATE_CLAIMED,
                podspec.LABEL_THREAD: thread,
            },
            annotations={podspec.ANNO_LAST_USED: str(int(time.time()))},
            phase="Running",
            pod_ip="10.42.0.99",
            ready=True,
            env={"AGENT_TOKEN": token},
            image="jarvis-sandbox:test",
        )
        self._pods[name] = pod
        return pod


@pytest.fixture
def fake_k8s():
    return FakeK8s()
