"""The agent-pod manifest must stay locked down. These assertions pin the
security posture so a future edit can't quietly loosen it.
"""

import os

os.environ.setdefault("SANDBOX_ROLE", "orchestrator")
os.environ.setdefault("SANDBOX_IMAGE", "jarvis-sandbox:test")

from app.orchestrator import podspec  # noqa: E402


def _spec():
    return podspec.build("reg/jarvis-sandbox:abc", "tok-123")["spec"]


def test_non_root_no_caps_seccomp():
    pod_sec = _spec()["securityContext"]
    assert pod_sec["runAsNonRoot"] is True
    assert pod_sec["runAsUser"] == 1000
    assert pod_sec["seccompProfile"] == {"type": "RuntimeDefault"}

    c_sec = _spec()["containers"][0]["securityContext"]
    assert c_sec["allowPrivilegeEscalation"] is False
    assert c_sec["capabilities"]["drop"] == ["ALL"]
    assert c_sec["readOnlyRootFilesystem"] is True


def test_no_serviceaccount_token_no_service_links():
    spec = _spec()
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert "serviceAccountName" not in spec


def test_only_emptydirs_are_writable():
    spec = _spec()
    mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert mounts == {"/workspace", "/tmp", "/var/tmp"}
    for v in spec["volumes"]:
        assert "emptyDir" in v  # never a hostPath / PVC / secret


def test_env_carries_no_secret_refs():
    env = _spec()["containers"][0]["env"]
    for e in env:
        assert "valueFrom" not in e  # only literal values reach the sandbox
    names = {e["name"] for e in env}
    assert "AGENT_TOKEN" in names
    assert "INTERNAL_API_KEY" not in names


def test_runtime_class_is_opt_in(monkeypatch):
    from app.core.config import settings

    assert "runtimeClassName" not in _spec()
    monkeypatch.setattr(settings, "SANDBOX_RUNTIME_CLASS", "gvisor")
    assert _spec()["runtimeClassName"] == "gvisor"
