from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Jarvis Sandbox"
    APP_VERSION: str = "0.1.0"
    API_PREFIX: str = "/api/v1"

    # Shared secret jarvis-backend sends on every call — this service is
    # internal-only (never reachable from the frontend/ingress), so a plain
    # header check is enough. Same posture as jarvis-conversation-service.
    INTERNAL_API_KEY: str = ""

    WORKSPACE_ROOT: str = "/workspace"
    COMMAND_TIMEOUT_SECONDS: int = 300
    IDLE_GC_MINUTES: int = 60

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()
