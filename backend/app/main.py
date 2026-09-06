"""SlideAI local-first FastAPI application.

Project state is persisted by ``artifact_store`` under ``data/video_runs``.
The retired multi-user SQL/authentication API is intentionally not mounted.
"""

from datetime import datetime, timezone
from contextlib import asynccontextmanager
import logging
import os
import shutil
import subprocess

import psutil
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.api import video, video_runs


def check_poppler_available() -> None:
    """Fail fast if PDF rendering support is unavailable."""
    logger = logging.getLogger("slideai.startup")
    poppler_path = os.getenv("POPPLER_PATH")
    if poppler_path and os.path.isfile(poppler_path) and os.access(poppler_path, os.X_OK):
        logger.info("POPPLER_PATH is available: %s", poppler_path)
        return
    if shutil.which("pdftoppm"):
        logger.info("pdftoppm found on PATH")
        return
    try:
        subprocess.run(
            ["pdftoppm", "-v"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:
        raise RuntimeError("pdftoppm (poppler-utils) is required") from exc


def recover_video_batch_queue() -> None:
    """Mark orphaned jobs as interrupted, awaiting the user's recovery decision."""
    recovered = video.recover_persistent_batch_jobs()
    if recovered:
        logging.getLogger("slideai.batch").info(
            "Marked %s orphaned batch render job(s) for recovery or cancellation", recovered
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_poppler_available()
    recover_video_batch_queue()
    yield


app = FastAPI(
    title="SlideAI Backend API",
    version="1.0.0",
    description="Local-first PDF narration and video rendering service.",
    lifespan=lifespan,
)
app.include_router(video.router)
app.include_router(video_runs.router)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
app.mount("/api/static", StaticFiles(directory=_static_dir), name="api-static")

allowed_hosts = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "*").split(",")
    if host.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["*"])

origins = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:5175",
    "http://127.0.0.1:5175",
    "http://localhost:3000",
    "https://awinlabnchu.github.io",
}
for value in os.getenv("FRONTEND_URL", "").split(","):
    if value.strip():
        origins.add(value.strip())
for name in ("CORS_ALLOW_1", "CORS_ALLOW_2", "CORS_ALLOW_3", "CORS_ALLOW_4"):
    value = os.getenv(name)
    if value:
        origins.add(value)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(origins),
    allow_origin_regex=os.getenv(
        "CORS_ALLOW_ORIGIN_REGEX",
        r"^https?://(?:localhost|127\.0\.0\.1|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})"
        r"(?::\d+)?$",
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)


@app.get("/")
def root() -> dict:
    return {
        "message": "SlideAI Backend API",
        "status": "running",
        "storage": "data/video_runs",
        "webui": "/video-abstract-lab",
    }


@app.get("/health")
@app.get("/api/health")
def health_check() -> dict:
    memory = psutil.virtual_memory()
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "storage": "filesystem",
        "memory_usage": {
            "percent": memory.percent,
            "available_mb": memory.available // 1024 // 1024,
            "total_mb": memory.total // 1024 // 1024,
        },
    }


@app.get("/api/test-cors")
def test_cors() -> dict:
    return {"status": "ok"}
