from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import router
from app.config import get_settings
from app.dependencies import get_automation_service, get_task_manager


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings()
    await get_automation_service().start()
    yield
    await get_automation_service().stop()
    await get_task_manager().shutdown()


app = FastAPI(
    title="Audio Separator Service",
    description="Local vocal separation service powered by python-audio-separator",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/")
async def root() -> dict:
    return {"service": "audio-separator-service", "docs": "/docs", "health": "/api/health"}
