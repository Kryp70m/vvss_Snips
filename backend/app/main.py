import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.alerts.telegram import TelegramAlerter
from app.api.routes import router as routes_router
from app.api.websocket import router as websocket_router
from app.core.config import get_settings
from app.marketdata.binance import BinanceFuturesClient
from app.persistence.cache import RedisCache
from app.persistence.postgres import PostgresStore
from app.services.access_control import AccessControl
from app.services.scanner import ScannerService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# Smart path resolution: Works perfectly both locally and inside the Docker container
CONTAINER_ROOT = Path("/app")
if CONTAINER_ROOT.exists() and (CONTAINER_ROOT / "spot-momentum-scanner.html").exists():
    PACKAGE_ROOT = CONTAINER_ROOT
else:
    PACKAGE_ROOT = Path(__file__).resolve().parents[2]

FRONTEND_HTML = PACKAGE_ROOT / "spot-momentum-scanner.html"
ADMIN_HTML = PACKAGE_ROOT / "admin.html"
MANIFEST_FILE = PACKAGE_ROOT / "manifest.webmanifest"
SERVICE_WORKER_FILE = PACKAGE_ROOT / "sw.js"
ASSETS_DIR = PACKAGE_ROOT / "assets"


async def wait_any(*awaitables):
    tasks = [asyncio.create_task(awaitable) for awaitable in awaitables]
    return await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    binance = BinanceFuturesClient(
        settings.binance_futures_ws_base,
        settings.binance_futures_rest_base,
        insecure_ssl=settings.binance_insecure_ssl,
        include_individual_trade_stream=settings.include_individual_trade_stream,
        include_book_ticker_stream=settings.include_book_ticker_stream,
        include_kline_stream=settings.include_kline_stream,
    )
    cache = RedisCache(settings.redis_url)
    store = PostgresStore(settings.database_url)
    telegram = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)
    scanner = ScannerService(settings, binance, cache, store, telegram)
    app.state.started_at = time.time()
    app.state.websocket_count = 0
    app.state.scanner = scanner
    app.state.telegram = telegram
    app.state.access = AccessControl(PACKAGE_ROOT)
    watchlist_setting = app.state.access.get_admin_setting("priority_watchlist", {"binance": [], "mexc": []})
    if isinstance(watchlist_setting, list):
        watchlist_setting = {"binance": watchlist_setting, "mexc": []}
    scanner.update_priority_watchlist(watchlist_setting if isinstance(watchlist_setting, dict) else {"binance": [], "mexc": []})
    app.state.wait_any = wait_any
    task = asyncio.create_task(scanner.start())
    try:
        yield
    finally:
        task.cancel()
        await scanner.stop()


# The server app instance is created here cleanly first
app = FastAPI(title="Binance Spot Momentum Ignition Scanner", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


PUBLIC_API_PATHS = {
    "/health",
    "/api/access/status",
    "/api/site-content",
    "/api/access/login",
    "/api/access/logout",
    "/api/admin/login",
}

PUBLIC_API_PREFIXES = (
    "/api/access/activate/",
    "/api/access/invite/",
)


@app.middleware("http")
async def access_gate(request: Request, call_next):
    path = request.url.path
    auth_header = request.headers.get("authorization", "")
    bearer = auth_header.split(" ", 1)[1].strip() if auth_header.lower().startswith("bearer ") else None
    if path.startswith("/api/admin/") and path != "/api/admin/login":
        session = (
            request.app.state.access.check_session(bearer, {"admin"})
            or request.app.state.access.check_session(request.cookies.get("alpha_admin_token"), {"admin"})
        )
        if not session:
            return JSONResponse({"detail": "Owner login required"}, status_code=401)
    elif path.startswith("/api/") and path not in PUBLIC_API_PATHS and not path.startswith(PUBLIC_API_PREFIXES):
        access = request.app.state.access
        user_session = access.check_session(bearer, {"user"}) or access.check_session(request.cookies.get("alpha_access_token"), {"user"})
        admin_session = access.check_session(bearer, {"admin"}) or access.check_session(request.cookies.get("alpha_admin_token"), {"admin"})
        if not user_session and not admin_session:
            return JSONResponse({"detail": "PIN login required"}, status_code=401)
    return await call_next(request)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_HTML)


@app.get("/spot-momentum-scanner.html", include_in_schema=False)
async def scanner_page() -> FileResponse:
    return FileResponse(FRONTEND_HTML)


@app.get("/admin.html", include_in_schema=False)
async def admin_page() -> FileResponse:
    return FileResponse(ADMIN_HTML)


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(MANIFEST_FILE, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(SERVICE_WORKER_FILE, media_type="application/javascript")


if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

app.include_router(routes_router)
app.include_router(websocket_router)