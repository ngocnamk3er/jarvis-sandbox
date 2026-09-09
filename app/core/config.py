from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Jarvis Sandbox"
    APP_VERSION: str = "0.1.0"
    API_PREFIX: str = "/api/v1"

    # Shared secret jarvis-backend sends on every call — this service is
    # internal-only (never reachable from the frontend/ingress), so a plain
    # header check is enough. Same posture as jarvis-conversation-service.
    INTERNAL_API_KEY: str = ""

    # emptyDir mount. Each conversation gets DATA_ROOT/<thread_id>/ (its
    # "prefix"), and DATA_ROOT/<thread_id>/workspace/ is bind-mounted to
    # /workspace inside a per-exec mount namespace — so the agent only ever
    # sees /workspace and can't reach a sibling thread's tree.
    DATA_ROOT: str = "/data"
    COMMAND_TIMEOUT_SECONDS: int = 300
    IDLE_GC_MINUTES: int = 60
    # per-thread uid range for setpriv (belt-and-braces alongside the mount ns)
    UID_BASE: int = 20000
    UID_RANGE: int = 40000

    # LOCAL DEV ONLY. The namespace jail (unshare/mount/setpriv) needs root +
    # CAP_SYS_ADMIN, which `make dev` on a laptop doesn't have. Set this to run
    # commands with NO isolation (just cwd) so the service is usable locally.
    # NEVER set it in a shared/prod deployment — it removes all sandboxing.
    UNSAFE_NO_JAIL: bool = False

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
