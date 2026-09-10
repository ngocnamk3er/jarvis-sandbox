"""The agent-pod manifest the orchestrator writes, plus the label/annotation
keys that make the pool state authoritative in the cluster (so an orchestrator
restart re-adopts running sandboxes instead of orphaning them).
"""

from app.core.config import settings

LABEL_APP = "app"
APP_VALUE = "jarvis-sandbox-agent"
LABEL_STATE = "jarvis.sandbox/state"  # "warm" | "claimed"
LABEL_THREAD = "jarvis.sandbox/thread"  # sanitised thread_id, only when claimed
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"

ANNO_LAST_USED = "jarvis.sandbox/last-used"  # unix seconds, bumped lazily by GC
ANNO_CLAIMED_AT = "jarvis.sandbox/claimed-at"

STATE_WARM = "warm"
STATE_CLAIMED = "claimed"


def build(image: str, token: str) -> dict:
    """A single-container, non-root, no-capabilities, no-serviceaccount pod.
    Created 'warm' (unclaimed); claim() patches the labels in place.
    """
    s = settings

    pod_security = {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        # primary group stays 0 (the image makes site-packages group-0
        # writable so `pip install` still works); fsGroup 1000 is added as a
        # supplementary group and owns the emptyDir mounts.
        "fsGroup": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    container_security = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": False,  # `pip install` writes site-packages
    }

    spec: dict = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,  # don't inject *_SERVICE_HOST env for every Service
        "restartPolicy": "Always",
        "terminationGracePeriodSeconds": 5,
        "securityContext": pod_security,
        "containers": [
            {
                "name": "agent",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(s.AGENT_PORT),
                ],
                "env": [
                    {"name": "SANDBOX_ROLE", "value": "agent"},
                    {"name": "AGENT_TOKEN", "value": token},
                    {"name": "WORKSPACE_DIR", "value": "/workspace"},
                    {"name": "COMMAND_TIMEOUT_SECONDS", "value": str(s.COMMAND_TIMEOUT_SECONDS)},
                    {"name": "API_PREFIX", "value": s.API_PREFIX},
                    {"name": "HOME", "value": "/workspace"},
                ],
                "ports": [{"containerPort": s.AGENT_PORT, "name": "http"}],
                "securityContext": container_security,
                "resources": {
                    "requests": {
                        "cpu": s.SANDBOX_CPU_REQUEST,
                        "memory": s.SANDBOX_MEM_REQUEST,
                    },
                    "limits": {
                        "cpu": s.SANDBOX_CPU_LIMIT,
                        "memory": s.SANDBOX_MEM_LIMIT,
                    },
                },
                "volumeMounts": [
                    {"name": "workspace", "mountPath": "/workspace"},
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
                "readinessProbe": {
                    "httpGet": {"path": f"{s.API_PREFIX}/health", "port": s.AGENT_PORT},
                    "initialDelaySeconds": 2,
                    "periodSeconds": 3,
                    "failureThreshold": 3,
                },
            }
        ],
        "volumes": [
            {"name": "workspace", "emptyDir": {"sizeLimit": s.SANDBOX_WORKSPACE_SIZE}},
            {"name": "tmp", "emptyDir": {"sizeLimit": s.SANDBOX_TMP_SIZE}},
        ],
    }
    if s.SANDBOX_RUNTIME_CLASS:
        spec["runtimeClassName"] = s.SANDBOX_RUNTIME_CLASS

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "generateName": "sbx-",
            "labels": {
                LABEL_APP: APP_VALUE,
                LABEL_STATE: STATE_WARM,
                LABEL_MANAGED_BY: "jarvis-sandbox-orchestrator",
            },
        },
        "spec": spec,
    }
