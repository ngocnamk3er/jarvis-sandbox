"""Thin async wrapper over the slice of the k8s API the pool needs.

Everything the orchestrator does to the cluster goes through here, and every
method returns a plain `PodView` dataclass rather than a client model object,
so `pool.py` and its tests share one shape and the tests can drop in a fake.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.rest import ApiException

logger = logging.getLogger(__name__)


@dataclass
class PodView:
    name: str
    labels: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)
    phase: str = "Pending"
    pod_ip: str | None = None
    ready: bool = False
    created_ts: float = field(default_factory=time.time)
    env: dict = field(default_factory=dict)  # container[0] literal env, name -> value
    image: str | None = None

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_ts)


def _view(pod) -> PodView:
    meta = pod.metadata
    status = pod.status
    containers = (pod.spec.containers or []) if pod.spec else []
    c0 = containers[0] if containers else None

    ready = False
    for cond in (status.conditions or []) if status else []:
        if cond.type == "Ready":
            ready = cond.status == "True"
            break

    env = {}
    if c0 and c0.env:
        for e in c0.env:
            if e.value is not None:  # literal values only, not secretKeyRef
                env[e.name] = e.value

    created = meta.creation_timestamp.timestamp() if meta.creation_timestamp else time.time()

    return PodView(
        name=meta.name,
        labels=dict(meta.labels or {}),
        annotations=dict(meta.annotations or {}),
        phase=(status.phase if status else "Pending") or "Pending",
        pod_ip=(status.pod_ip if status else None),
        ready=ready,
        created_ts=created,
        env=env,
        image=(c0.image if c0 else None),
    )


class K8sClient:
    def __init__(self, namespace: str):
        self.namespace = namespace
        self._api: client.CoreV1Api | None = None
        self._raw: client.ApiClient | None = None

    async def start(self) -> None:
        try:
            config.load_incluster_config()
            logger.info("k8s: in-cluster config")
        except config.ConfigException:
            await config.load_kube_config()
            logger.info("k8s: kubeconfig")
        self._raw = client.ApiClient()
        self._api = client.CoreV1Api(self._raw)

    async def close(self) -> None:
        if self._raw is not None:
            await self._raw.close()

    async def list_pods(self, label_selector: str) -> list[PodView]:
        resp = await self._api.list_namespaced_pod(self.namespace, label_selector=label_selector)
        return [_view(p) for p in resp.items]

    async def read_pod(self, name: str) -> PodView | None:
        try:
            return _view(await self._api.read_namespaced_pod(name, self.namespace))
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def create_pod(self, manifest: dict) -> PodView:
        return _view(await self._api.create_namespaced_pod(self.namespace, manifest))

    async def delete_pod(self, name: str) -> None:
        try:
            await self._api.delete_namespaced_pod(name, self.namespace, grace_period_seconds=5)
        except ApiException as e:
            if e.status != 404:
                raise

    async def patch_pod_metadata(
        self,
        name: str,
        labels: dict | None = None,
        annotations: dict | None = None,
    ) -> None:
        meta: dict = {}
        if labels is not None:
            meta["labels"] = labels
        if annotations is not None:
            meta["annotations"] = annotations
        try:
            await self._api.patch_namespaced_pod(name, self.namespace, {"metadata": meta})
        except ApiException as e:
            if e.status != 404:
                raise
