from fastapi import APIRouter

from app.api.v1.endpoints import exec, health

router = APIRouter()

router.include_router(health.router, prefix="/health", tags=["health"])
router.include_router(exec.router, prefix="/sandbox", tags=["sandbox"])
