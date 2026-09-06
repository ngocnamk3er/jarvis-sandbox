from fastapi import Header, HTTPException

from app.core.config import settings


async def verify_internal_key(x_internal_api_key: str = Header(default="")) -> None:
    """Internal-only service — reachable only from jarvis-backend over the
    cluster network. A shared secret header is enough; same pattern as
    jarvis-conversation-service / jarvis-file-service."""
    if not settings.INTERNAL_API_KEY or x_internal_api_key != settings.INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing internal API key")
