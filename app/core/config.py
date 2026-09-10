from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Jarvis Sandbox"
    APP_VERSION: str = "0.2.0"
    API_PREFIX: str = "/api/v1"

    # Which half of the image this process is:
    #   "orchestrator" — the control plane. Keeps a warm pool of agent pods,
    #                    maps thread_id -> pod, proxies exec/read, deletes pods
    #                    on reset / idle GC. This is what jarvis-backend talks
    #                    to (Service `sandbox`, unchanged URL).
    #   "agent"        — runs *inside* one sandbox pod. One pod == one
    #                    conversation, so there is no in-process jail: the pod
    #                    (its own PID/net/mount ns, non-root, dropped caps,
    #                    seccomp, a NetworkPolicy) is the isolation boundary.
    # One image, two roles. Local `make dev` runs "agent" (bash in a dir, no
    # cluster needed) — same "fine for one developer" posture as the old
    # UNSAFE_NO_JAIL.
    SANDBOX_ROLE: str = "agent"

    # Shared secret jarvis-backend sends on every call to the orchestrator's
    # public API. The orchestrator is internal-only (never reachable from the
    # ingress); a header check is enough, same as conversation/file-service.
    INTERNAL_API_KEY: str = ""

    # ------------------------------------------------------------------ agent
    # The one directory a sandbox pod exposes to the command; an emptyDir in
    # k8s, any writable dir locally.
    WORKSPACE_DIR: str = "/workspace"
    COMMAND_TIMEOUT_SECONDS: int = 300
    # Per-pod bearer token the orchestrator injects (AGENT_TOKEN env) and
    # replays on every call. Scoped to this one pod, so a command reading it
    # out of /proc/self/environ gains nothing. Empty -> no check (local dev).
    AGENT_TOKEN: str = ""

    # ----------------------------------------------------------- orchestrator
    POD_NAMESPACE: str = "jarvis"
    # Image to run agent pods with. Empty -> the orchestrator reads its own
    # running image off the k8s API at startup, so the two never drift and CI
    # only has to bump one tag.
    SANDBOX_IMAGE: str = ""
    # Pool sizing. Default is pure on-demand: no idle pods are kept around, a
    # pod is created when a conversation first needs one. Raise POOL_SIZE to
    # keep that many pre-warmed pods Ready (hides the ~3-5s pod start on the
    # first bash call), at the cost of that many idle pods holding requests.
    POOL_SIZE: int = 0
    MAX_SANDBOXES: int = 6  # hard ceiling on total agent pods (capacity guard)
    # A pod is deleted when EITHER: no bash call has touched it for
    # IDLE_GC_MINUTES, OR it has simply existed for SANDBOX_TTL_MINUTES (a hard
    # per-pod lifetime cap — the conversation gets a fresh pod on its next
    # call, same as an idle reap or a pod restart).
    IDLE_GC_MINUTES: int = 30
    SANDBOX_TTL_MINUTES: int = 180
    CLAIM_TIMEOUT_SECONDS: int = 40  # wait budget for a new pod to become Ready
    AGENT_PORT: int = 8000
    RECONCILE_INTERVAL_SECONDS: int = 30  # warm-pool top-up cadence (no-op when POOL_SIZE=0)
    GC_INTERVAL_SECONDS: int = 300

    # agent pod sizing (the hypervisor-less equivalent of E2B's vCPU/RAM knobs)
    SANDBOX_CPU_REQUEST: str = "100m"
    SANDBOX_CPU_LIMIT: str = "2"
    SANDBOX_MEM_REQUEST: str = "256Mi"
    SANDBOX_MEM_LIMIT: str = "2Gi"
    SANDBOX_WORKSPACE_SIZE: str = "2Gi"
    SANDBOX_TMP_SIZE: str = "1Gi"
    # Set to "gvisor" (or "kata") once the RuntimeClass exists on the cluster
    # for kernel-level isolation. Empty -> the cluster's default runtime.
    SANDBOX_RUNTIME_CLASS: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = True
        # Tolerate leftover keys (old DATA_ROOT / UNSAFE_NO_JAIL, or a
        # configmap mid-rollout) instead of crashing on startup.
        extra = "ignore"


settings = Settings()
