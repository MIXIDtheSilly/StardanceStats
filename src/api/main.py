from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, ORJSONResponse

from .. import __version__, db as database
from ..config import settings
from .docs import DESCRIPTION, TAGS, scalar_page
from .middleware import cache_headers
from .routers import ask, devlogs, health, platform, projects, shop, users
from .services.ask import close as close_ask

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)


log = logging.getLogger(__name__)


def _json_response() -> type[JSONResponse]:
    """ORJSONResponse only checks for orjson when it renders, which 500s every read."""
    try:
        import orjson  # noqa: F401
    except ImportError:
        log.warning("orjson is not installed; encoding responses the slower way")
        return JSONResponse
    return ORJSONResponse


def _log_ask_config() -> None:
    missing = []
    if not settings.ask_api_key:
        missing.append("STARDANCE_ASK_API_KEY")
    if not settings.ask_mongo_url:
        missing.append("STARDANCE_ASK_MONGO_URL")
    if missing:
        log.warning(
            "Ask disabled, /v1/ask will answer 503; unset: %s", ", ".join(missing)
        )
        return
    log.info(
        "Ask enabled: model=%s api=%s callers=%s rate=%s/h",
        settings.ask_model,
        settings.ask_api_url,
        ",".join(sorted(settings.ask_caller_list)),
        settings.ask_rate_limit,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.bootstrap()
    _log_ask_config()
    yield
    await close_ask()
    await database.close()


app = FastAPI(
    title=settings.api_title,
    version=__version__,
    summary="Public time-series statistics for the Stardance platform.",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    license_info={"name": "MIT", "identifier": "MIT"},
    # Scalar replaces the stock page at /docs; Redoc stays as a CDN-free fallback.
    docs_url=None,
    # A ranking page or a history window is a lot of numbers to encode in Python.
    default_response_class=_json_response(),
    lifespan=lifespan,
)

# First, so CORS ends up outside it: a cache hit never reaches the app.
app.middleware("http")(cache_headers)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["ETag"],
)

app.include_router(health.router, prefix="/v1", tags=["meta"])
app.include_router(platform.router, prefix="/v1", tags=["global"])
app.include_router(projects.router, prefix="/v1", tags=["projects"])
app.include_router(devlogs.router, prefix="/v1", tags=["devlogs"])
app.include_router(users.router, prefix="/v1", tags=["users"])
app.include_router(shop.router, prefix="/v1", tags=["shop"])
# Served through the site, not the public API, so it stays out of the schema.
app.include_router(ask.router, prefix="/v1", tags=["ask"], include_in_schema=False)


@app.get("/docs", include_in_schema=False)
async def docs() -> HTMLResponse:
    title = f"{settings.api_title} reference"
    return scalar_page(app.openapi_url or "/openapi.json", title)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": settings.api_title, "version": __version__, "docs": "/docs"}
