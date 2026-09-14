from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Jarvis Sandbox"
    APP_VERSION: str = "0.2.0"
    API_PREFIX: str = "/api/v1"

    # The one directory a sandbox pod exposes to the command — an emptyDir in
    # k8s (mounted by the SandboxTemplate), any writable dir locally.
    WORKSPACE_DIR: str = "/workspace"

    class Config:
        env_file = ".env"
        case_sensitive = True
        # Tolerate leftover keys (old orchestrator/agent-role settings from
        # before the agent-sandbox cutover, or a configmap mid-rollout)
        # instead of crashing on startup.
        extra = "ignore"


settings = Settings()
