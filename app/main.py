import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.core.config import settings
from app.api.v1.router import router as api_v1_router
from app.services import runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.WORKSPACE_ROOT).mkdir(parents=True, exist_ok=True)
    gc_task = asyncio.create_task(runner.gc_loop())
    yield
    gc_task.cancel()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.include_router(api_v1_router, prefix=settings.API_PREFIX)


@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.APP_NAME}"}
