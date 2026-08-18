from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__, db as database
from ..config import settings
from .middleware import cache_headers
from .routers import ask, devlogs, health, platform, projects, shop, users
from .services.ask import close as close_ask

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.bootstrap()
    yield
    await close_ask()
    await database.close()


app = FastAPI(
    title=settings.api_title,
    version=__version__,
    description=(
        "Time-series statistics for the Stardance platform.\n\n"
        "All figures are derived from Stardance's **public** pages. Rejected "
        "ships, deleted devlogs and unverified profiles are not visible to us, "
        "so these numbers reflect the public view rather than platform truth."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)

app.middleware("http")(cache_headers)

app.include_router(health.router, prefix="/v1", tags=["meta"])
app.include_router(platform.router, prefix="/v1", tags=["global"])
app.include_router(projects.router, prefix="/v1", tags=["projects"])
app.include_router(devlogs.router, prefix="/v1", tags=["projects"])
app.include_router(users.router, prefix="/v1", tags=["users"])
app.include_router(shop.router, prefix="/v1", tags=["shop"])
app.include_router(ask.router, prefix="/v1", tags=["ask"])


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": settings.api_title, "version": __version__, "docs": "/docs"}
