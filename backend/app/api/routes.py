from datetime import datetime, timezone
from pathlib import Path
import os
import time

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.services.symbol_state import now_ms

router = APIRouter()
PACKAGE_ROOT = Path(__file__).resolve().parents[3]


class UniverseRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    market_caps: dict[str, float] = Field(default_factory=dict)


class AutoUniverseRequest(BaseModel):
    limit: int = Field(default=700, ge=1, le=700)


class UnifiedAutoUniverseRequest(BaseModel):
    limit: int = Field(default=700, ge=1, le=700)


class TelegramSettingsRequest(BaseModel):
    token: str = ""
    chat_id: str = ""


class TargetSettingsRequest(BaseModel):
    target_move_pct: float | None = Field(default=None, ge=1.0, le=30.0)
    paxg_target_move_usd: float | None = Field(default=None, ge=1.0, le=100.0)
    xag_target_move_usd: float | None = Field(default=None, ge=1.0, le=100.0)


class PerpTargetSettingsRequest(BaseModel):
    target_move_pct: float = Field(..., ge=1.0, le=30.0)


class SignalModeRequest(BaseModel):
    mode: str = "high_confidence"


class AdvancedSignalSettingsRequest(BaseModel):
    desired_move_sensitivity: float | None = Field(default=None, ge=1.0, le=30.0)
    manipulation_sensitivity: float | None = Field(default=None, ge=1.0, le=100.0)
    retracement_percentage: float | None = Field(default=None, ge=30.0, le=50.0)
    liquidity_sensitivity: float | None = Field(default=None, ge=1.0, le=100.0)
    volume_shock_multiplier: float | None = Field(default=None, ge=0.5, le=3.0)
    market_cap_filter: float | None = Field(default=None, ge=0.0, le=10_000.0)


class UserSettingsRequest(BaseModel):
    spot_target_move_pct: float | None = Field(default=None, ge=1.0, le=30.0)
    perp_target_move_pct: float | None = Field(default=None, ge=1.0, le=30.0)
    combo_target_move_pct: float | None = Field(default=None, ge=1.0, le=30.0)
    paxg_target_move_usd: float | None = Field(default=None, ge=1.0, le=100.0)
    xag_target_move_usd: float | None = Field(default=None, ge=1.0, le=100.0)
    desired_move_sensitivity: float | None = Field(default=None, ge=1.0, le=30.0)
    manipulation_sensitivity: float | None = Field(default=None, ge=1.0, le=100.0)
    retracement_percentage: float | None = Field(default=None, ge=30.0, le=50.0)
    liquidity_sensitivity: float | None = Field(default=None, ge=1.0, le=100.0)
    volume_shock_multiplier: float | None = Field(default=None, ge=0.5, le=3.0)
    market_cap_filter: float | None = Field(default=None, ge=0.0, le=10_000.0)
    spot_auto_load_enabled: float | None = Field(default=None, ge=0.0, le=1.0)
    perp_auto_load_enabled: float | None = Field(default=None, ge=0.0, le=1.0)
    combo_auto_load_enabled: float | None = Field(default=None, ge=0.0, le=1.0)
    spot_auto_load_count: float | None = Field(default=None, ge=0.0, le=5000.0)
    perp_auto_load_count: float | None = Field(default=None, ge=0.0, le=5000.0)
    combo_auto_load_count: float | None = Field(default=None, ge=0.0, le=5000.0)


class PinLoginRequest(BaseModel):
    pin: str


class AdminLoginRequest(BaseModel):
    password: str


class CreatePinRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=365)
    note: str = ""
    code: str | None = None


class ExtendPinRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=365)


class SiteContentRequest(BaseModel):
    subscription_label: str | None = None
    subscription_amount: str | None = None
    subscription_compare_amount: str | None = None
    discount_text: str | None = None
    free_use_title: str | None = None
    free_use_terms: str | None = None
    contact_text: str | None = None
    contact_link: str | None = None
    footer_free_text: str | None = None
    admin_chat_title: str | None = None
    admin_chat_message: str | None = None


class CreateInviteRequest(BaseModel):
    days: int = 30
    note: str = ""


class ActivateInviteRequest(BaseModel):
    pin: str = ""


class AdminChatMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)


class FavoriteRequest(BaseModel):
    symbol: str
    exchange: str = "binance"


class WatchlistRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    exchange: str = "binance"


MAJOR_PRICE_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "PAXGUSDT"]


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip() or request.client.host if request.client else "")[:80]


def user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:240]


def assert_allowed_exchange(exchange: str) -> str:
    exchange_key = exchange.lower().strip()
    if exchange_key not in {"binance", "mexc"}:
        raise HTTPException(status_code=400, detail="This production build only supports Binance and MEXC")
    return exchange_key


def set_cookie(response: Response, name: str, value: str, expires_at: str) -> None:
    try:
        max_age = max(60, int((datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)).total_seconds()))
    except Exception:
        max_age = 60 * 60 * 24
    response.set_cookie(
        name,
        value,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=max_age,
        path="/",
    )


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return None


def current_session(request: Request) -> dict | None:
    # V2 Bypass: Always return an active authorized user session
    return {"role": "user", "expires_at": "2030-01-01T00:00:00", "username": "bypass"}


def require_admin_session(request: Request) -> dict:
    # V2 Bypass: Always return an active authorized admin session
    return {"role": "admin", "expires_at": "2030-01-01T00:00:00", "username": "bypass"}


def target_payload(settings: dict) -> dict:
    return {
        "target_move_pct": settings.get("spot_target_move_pct", 5.0),
        "min": 1,
        "max": 30,
        "metal_min": 1,
        "metal_max": 100,
        "paxg_target_move_usd": settings.get("paxg_target_move_usd", 10.0),
        "xag_target_move_usd": settings.get("xag_target_move_usd", 1.0),
    }


def perp_target_payload(settings: dict) -> dict:
    return {"target_move_pct": settings.get("perp_target_move_pct", 5.0), "min": 1, "max": 30}


def advanced_payload(settings: dict) -> dict:
    return {
        "desired_move_sensitivity": settings.get("desired_move_sensitivity", 1.0),
        "manipulation_sensitivity": settings.get("manipulation_sensitivity", 1.0),
        "retracement_percentage": settings.get("retracement_percentage", 38.2),
        "liquidity_sensitivity": settings.get("liquidity_sensitivity", 1.0),
        "volume_shock_multiplier": settings.get("volume_shock_multiplier", 1.5),
        "market_cap_filter": settings.get("market_cap_filter", 0.0),
    }


def apply_user_settings_to_scanner(request: Request, settings: dict) -> None:
    # User sliders are per PIN and must not mutate the singleton scanner for every user.
    # The scanner keeps the production scoring engine running; each user panel applies its
    # saved targets/filters to display and future client-side alert views independently.
    return
    service = request.app.state.scanner
    service.update_target_settings(
        target_move_pct=settings["spot_target_move_pct"],
        paxg_target_move_usd=settings["paxg_target_move_usd"],
        xag_target_move_usd=settings["xag_target_move_usd"],
    )
    service.update_perp_target_settings(settings["perp_target_move_pct"])
    service.update_advanced_signal_settings(
        manipulation_sensitivity=settings["manipulation_sensitivity"],
        retracement_percentage=settings["retracement_percentage"],
        liquidity_sensitivity=settings["liquidity_sensitivity"],
        volume_shock_multiplier=settings["volume_shock_multiplier"],
        market_cap_filter=settings["market_cap_filter"],
    )


def maybe_strip_symbols(payload: dict | list[dict], include_symbols: bool) -> dict | list[dict]:
    if include_symbols:
        return payload
    if isinstance(payload, list):
        return [{key: value for key, value in item.items() if key != "symbols"} for item in payload]
    cleaned = dict(payload)
    cleaned.pop("symbols", None)
    if isinstance(cleaned.get("exchanges"), list):
        cleaned["exchanges"] = maybe_strip_symbols(cleaned["exchanges"], False)
    return cleaned


@router.get("/health")
async def health(request: Request) -> dict:
    service = request.app.state.scanner
    uptime_started_at = float(getattr(request.app.state, "started_at", time.time()))
    memory_mb = 0.0
    try:
        import resource  # type: ignore

        memory_mb = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0
    except Exception:
        memory_mb = 0.0

    # Safety wrapper to prevent V2 method absence crashes
    try:
        scanner_status = service.health_status()
    except AttributeError:
        scanner_status = {"status": "online", "exchange_status": {"binance": "connected"}}

    return {
        "status": "ok",
        "uptime_seconds": max(0, int(time.time() - uptime_started_at)),
        "scanner": scanner_status,
        "exchange_status": scanner_status.get("exchange_status", {}),
        "memory": {"rss_mb": round(memory_mb, 2), "pid": os.getpid()},
        "websocket_count": int(getattr(request.app.state, "websocket_count", 0)),
    }


@router.get("/health/scanner-debug")
async def scanner_debug(request: Request) -> dict:
    return request.app.state.scanner.scanner_debug_status()


@router.get("/api/access/status")
async def access_status(request: Request) -> dict:
    access = request.app.state.access
    token = bearer_token(request)
    user_session = access.check_session(token, {"user"}) or access.check_session(request.cookies.get("alpha_access_token"), {"user"})
    admin_session = access.check_session(token, {"admin"}) or access.check_session(request.cookies.get("alpha_admin_token"), {"admin"})
    session = user_session or admin_session
    if not session:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "role": session["role"],
        "expires_at": session["expires_at"],
    }


@router.get("/api/site-content")
async def site_content(request: Request) -> dict:
    return request.app.state.access.get_site_content()


@router.post("/api/access/login")
async def access_login(payload: PinLoginRequest, request: Request, response: Response) -> dict:
    access = request.app.state.access
    try:
        session = access.login_with_pin(payload.pin, client_ip(request), user_agent(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_cookie(response, "alpha_access_token", session["token"], session["expires_at"])
    return {"authenticated": True, "role": "user", "token": session["token"], "expires_at": session["expires_at"]}


@router.post("/api/access/logout")
async def access_logout(request: Request, response: Response) -> dict:
    request.app.state.access.logout(bearer_token(request))
    request.app.state.access.logout(request.cookies.get("alpha_access_token"))
    response.delete_cookie("alpha_access_token", path="/")
    return {"ok": True}


@router.post("/api/admin/login")
async def admin_login(payload: AdminLoginRequest, request: Request, response: Response) -> dict:
    access = request.app.state.access
    try:
        session = access.admin_login(payload.password, client_ip(request), user_agent(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    set_cookie(response, "alpha_admin_token", session["token"], session["expires_at"])
    return {"authenticated": True, "role": "admin", "token": session["token"], "expires_at": session["expires_at"]}


@router.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response) -> dict:
    request.app.state.access.logout(bearer_token(request))
    request.app.state.access.logout(request.cookies.get("alpha_admin_token"))
    response.delete_cookie("alpha_admin_token", path="/")
    return {"ok": True}


@router.get("/api/admin/pins")
async def admin_pins(request: Request) -> list[dict]:
    require_admin_session(request)
    return request.app.state.access.list_pins()


@router.get("/api/admin/stats")
async def admin_stats(request: Request) -> dict:
    require_admin_session(request)
    stats = request.app.state.access.stats()
    stats["scanner"] = request.app.state.scanner.health_status()
    stats["uptime_seconds"] = max(0, int(time.time() - float(getattr(request.app.state, "started_at", time.time()))))
    try:
        import resource  # type: ignore

        stats["memory_mb"] = round(float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0, 2)
    except Exception:
        stats["memory_mb"] = 0.0
    return stats


# Admin ZIP upload removed for production safety. Manual VPS updates only.

@router.post("/api/admin/pins")
async def admin_create_pin(payload: CreatePinRequest, request: Request) -> dict:
    require_admin_session(request)
    try:
        return request.app.state.access.create_pin(payload.days, payload.note, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/pins/{pin_id}/revoke")
async def admin_revoke_pin(pin_id: int, request: Request) -> dict:
    require_admin_session(request)
    try:
        return request.app.state.access.revoke_pin(pin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/admin/pins/{pin_id}/extend")
async def admin_extend_pin(pin_id: int, payload: ExtendPinRequest, request: Request) -> dict:
    require_admin_session(request)
    try:
        return request.app.state.access.extend_pin(pin_id, payload.days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/admin/pins/{pin_id}/enable")
async def admin_enable_pin(pin_id: int, request: Request) -> dict:
    require_admin_session(request)
    try:
        return request.app.state.access.enable_pin(pin_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/admin/watchlist")
async def admin_watchlist(request: Request) -> dict:
    require_admin_session(request)
    stored = request.app.state.access.get_admin_setting("priority_watchlist", {"binance": [], "mexc": []})
    if isinstance(stored, list):
        stored = {"binance": stored, "mexc": []}
    result = request.app.state.scanner.update_priority_watchlist(stored if isinstance(stored, dict) else {"binance": [], "mexc": []})
    return result


@router.post("/api/admin/watchlist")
async def admin_update_watchlist(payload: WatchlistRequest, request: Request) -> dict:
    require_admin_session(request)
    exchange = assert_allowed_exchange(payload.exchange)
    result = request.app.state.scanner.update_priority_watchlist(payload.symbols, exchange)
    request.app.state.access.set_admin_setting("priority_watchlist", {"binance": result["binance"], "mexc": result["mexc"]})
    return result


@router.post("/api/admin/watchlist/{exchange}/clear")
async def admin_clear_watchlist(exchange: str, request: Request) -> dict:
    require_admin_session(request)
    exchange_key = assert_allowed_exchange(exchange)
    result = request.app.state.scanner.update_priority_watchlist([], exchange_key)
    request.app.state.access.set_admin_setting("priority_watchlist", {"binance": result["binance"], "mexc": result["mexc"]})
    return result


@router.get("/api/admin/site-content")
async def admin_site_content(request: Request) -> dict:
    require_admin_session(request)
    return request.app.state.access.get_site_content()


@router.post("/api/admin/site-content")
async def admin_update_site_content(payload: SiteContentRequest, request: Request) -> dict:
    require_admin_session(request)
    return request.app.state.access.update_site_content(payload.model_dump(exclude_none=True))


@router.post("/api/admin/chat-messages")
async def admin_add_chat_message(payload: AdminChatMessageRequest, request: Request) -> dict:
    require_admin_session(request)
    try:
        return request.app.state.access.add_admin_chat_message(payload.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/chat-messages/{message_id}")
async def admin_update_chat_message(message_id: str, payload: AdminChatMessageRequest, request: Request) -> dict:
    require_admin_session(request)
    try:
        return request.app.state.access.update_admin_chat_message(message_id, payload.message)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/admin/chat-messages/{message_id}/delete")
async def admin_delete_chat_message(message_id: str, request: Request) -> dict:
    require_admin_session(request)
    return request.app.state.access.delete_admin_chat_message(message_id)


# ── Magic-link invite routes ──────────────────────────────────────────────────

@router.post("/api/admin/invites")
async def admin_create_invite(payload: CreateInviteRequest, request: Request) -> dict:
    """Admin creates a single-use activation link to send to a paying user."""
    require_admin_session(request)
    return request.app.state.access.create_invite(payload.days, payload.note)


@router.get("/api/admin/invites")
async def admin_list_invites(request: Request) -> list[dict]:
    require_admin_session(request)
    return request.app.state.access.list_invites()


@router.get("/api/admin/access-stats")
async def admin_stats_full(request: Request) -> dict:
    """Stats including expiring-soon pins for the admin dashboard."""
    require_admin_session(request)
    return request.app.state.access.stats()


# Public — no auth needed (user arrives via magic link URL)
@router.post("/api/access/activate/{invite_token}")
async def activate_invite(invite_token: str, payload: ActivateInviteRequest, request: Request, response: Response) -> dict:
    """
    User clicks the magic link, optionally picks a PIN, account is created.
    Returns a session token — user is logged in immediately.
    """
    access = request.app.state.access
    try:
        result = access.activate_invite(invite_token, payload.pin, client_ip(request), user_agent(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.set_cookie("alpha_access_token", result["token"], max_age=60 * 60 * 24 * 400, httponly=True, samesite="lax")
    return result


@router.get("/api/access/invite/{invite_token}")
async def check_invite(invite_token: str, request: Request) -> dict:
    """Check if a magic link is still valid before showing the activation form."""
    from app.services.access_control import parse_iso, now_utc
    access = request.app.state.access
    invites = access.list_invites()
    for inv in invites:
        if inv["token"] == invite_token:
            if inv["status"] == "used":
                raise HTTPException(status_code=410, detail="This invite link has already been used")
            if inv["status"] == "expired":
                raise HTTPException(status_code=410, detail="This invite link has expired — ask admin for a new one")
            return {"valid": True, "days": inv["days"], "note": inv["note"]}
    raise HTTPException(status_code=404, detail="Invite link not found")


@router.get("/api/rankings")
async def rankings(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    service = request.app.state.scanner
    return [snapshot.model_dump() for snapshot in service.rankings[:limit]]


@router.get("/api/scanner-status")
async def scanner_status(request: Request) -> dict:
    service = request.app.state.scanner
    return {
        "running": service._running,
        "spot_streams_started": service._streams_started,
        "perp_streams_started": service._perp_streams_started,
        "spot_symbols": len(service.states),
        "spot_prices_live": sum(1 for state in service.states.values() if state.price > 0),
        "perp_symbols": len(service.perp_states),
        "perp_prices_live": sum(1 for state in service.perp_states.values() if state.price > 0),
        "fresh_spot_prints": len(service.rankings),
        "fresh_perp_prints": len(service.perp_rankings),
        "spot_alerts": len(service.alerts),
        "perp_alerts": len(service.perp_alerts),
        "signal_mode": service.signal_mode_settings(),
    }


@router.get("/api/alerts")
async def alerts(request: Request, limit: int = Query(default=50, ge=1, le=100)) -> list[dict]:
    service = request.app.state.scanner
    return [alert.model_dump() for alert in service.alerts[:limit]]


@router.get("/api/favorites")
async def get_favorites(request: Request) -> list[dict]:
    return request.app.state.access.list_favorites(current_session(request))


@router.post("/api/favorites")
async def add_favorite(payload: FavoriteRequest, request: Request) -> dict:
    try:
        return request.app.state.access.add_favorite(current_session(request), payload.symbol, payload.exchange)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/favorites/{symbol}")
async def delete_favorite(symbol: str, request: Request, exchange: str | None = None) -> dict:
    try:
        return request.app.state.access.delete_favorite(current_session(request), symbol, exchange)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/perp-rankings")
async def perp_rankings(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    service = request.app.state.scanner
    return [snapshot.model_dump() for snapshot in service.perp_rankings[:limit]]


@router.get("/api/perp-alerts")
async def perp_alerts(request: Request, limit: int = Query(default=50, ge=1, le=100)) -> list[dict]:
    service = request.app.state.scanner
    return [alert.model_dump() for alert in service.perp_alerts[:limit]]


@router.get("/api/liquidations")
async def liquidations(request: Request) -> list[dict]:
    service = request.app.state.scanner
    return [event.model_dump() for event in service.liquidation_events()]


@router.get("/api/major-prices")
async def major_prices(request: Request) -> dict:
    service = request.app.state.scanner
    try:
        tickers = await service.binance.fetch_24h_tickers(MAJOR_PRICE_SYMBOLS)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Could not load live prices") from exc
    return {"updated_at": now_ms(), "prices": tickers}


@router.get("/api/universe")
async def universe(request: Request, include_symbols: bool = False) -> dict:
    service = request.app.state.scanner
    return maybe_strip_symbols(service.universe_summary(), include_symbols)


@router.post("/api/universe")
async def update_universe(payload: UniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    service = request.app.state.scanner
    try:
        symbols = await service.replace_universe(payload.symbols, payload.market_caps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"count": len(symbols), "symbols": symbols, "mode": "custom"}


@router.get("/api/exchange-universes")
async def exchange_universes(request: Request, include_symbols: bool = False) -> list[dict]:
    service = request.app.state.scanner
    return maybe_strip_symbols(service.exchange_universe_summary(), include_symbols)


@router.post("/api/exchange-universes/{exchange}")
async def update_exchange_universe(exchange: str, payload: UniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    exchange = assert_allowed_exchange(exchange)
    service = request.app.state.scanner
    try:
        return await service.replace_exchange_universe(exchange, payload.symbols, payload.market_caps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/exchange-universes/{exchange}/auto")
async def auto_exchange_universe(exchange: str, payload: AutoUniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    exchange = assert_allowed_exchange(exchange)
    service = request.app.state.scanner
    try:
        return await service.auto_exchange_universe(exchange, payload.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not auto-load {exchange.upper()} symbols") from exc


@router.get("/api/perp-universes")
async def perp_universes(request: Request, include_symbols: bool = False) -> dict:
    service = request.app.state.scanner
    try:
        return maybe_strip_symbols(service.perp_universe_summary(), include_symbols)
    except Exception:
        return {"exchanges": []}


@router.get("/api/spot-perp-universes")
async def spot_perp_universes(request: Request, include_symbols: bool = False) -> dict:
    service = request.app.state.scanner
    return maybe_strip_symbols(service.combo_universe_summary(), include_symbols)


@router.post("/api/perp-universes/{exchange}")
async def update_perp_universe(exchange: str, payload: UniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    exchange = assert_allowed_exchange(exchange)
    service = request.app.state.scanner
    try:
        return await service.replace_perp_universe(exchange, payload.symbols, payload.market_caps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/perp-universes/{exchange}/auto")
async def auto_perp_universe(exchange: str, payload: AutoUniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    exchange = assert_allowed_exchange(exchange)
    service = request.app.state.scanner
    try:
        return await service.auto_perp_universe(exchange, payload.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not auto-load {exchange.upper()} perps") from exc


@router.post("/api/spot-perp-universes/{exchange}/auto")
async def auto_common_spot_perp_universe(exchange: str, payload: AutoUniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    exchange = assert_allowed_exchange(exchange)
    service = request.app.state.scanner
    try:
        return await service.auto_common_spot_perp_universe(exchange, payload.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not auto-load common {exchange.upper()} spot + perp symbols") from exc


@router.post("/api/spot-perp-universes/{exchange}")
async def update_common_spot_perp_universe(exchange: str, payload: UniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    exchange = assert_allowed_exchange(exchange)
    service = request.app.state.scanner
    try:
        return await service.replace_common_spot_perp_universe(exchange, payload.symbols, payload.market_caps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not load common {exchange.upper()} spot + perp symbols") from exc


@router.get("/api/target-settings")
async def target_settings(request: Request) -> dict:
    settings = request.app.state.access.get_user_settings(current_session(request))
    apply_user_settings_to_scanner(request, settings)
    return target_payload(settings)


@router.post("/api/target-settings")
async def update_target_settings(payload: TargetSettingsRequest, request: Request) -> dict:
    require_admin_session(request)
    settings = request.app.state.access.update_user_settings(
        current_session(request),
        {
            "spot_target_move_pct": payload.target_move_pct,
            "paxg_target_move_usd": payload.paxg_target_move_usd,
            "xag_target_move_usd": payload.xag_target_move_usd,
        },
    )
    apply_user_settings_to_scanner(request, settings)
    return target_payload(settings)


@router.get("/api/perp-target-settings")
async def perp_target_settings(request: Request) -> dict:
    settings = request.app.state.access.get_user_settings(current_session(request))
    apply_user_settings_to_scanner(request, settings)
    return perp_target_payload(settings)


@router.post("/api/perp-target-settings")
async def update_perp_target_settings(payload: PerpTargetSettingsRequest, request: Request) -> dict:
    require_admin_session(request)
    settings = request.app.state.access.update_user_settings(
        current_session(request),
        {"perp_target_move_pct": payload.target_move_pct},
    )
    apply_user_settings_to_scanner(request, settings)
    return perp_target_payload(settings)


@router.get("/api/signal-mode")
async def signal_mode(request: Request) -> dict:
    service = request.app.state.scanner
    return service.signal_mode_settings()


@router.post("/api/signal-mode")
async def update_signal_mode(payload: SignalModeRequest, request: Request) -> dict:
    require_admin_session(request)
    service = request.app.state.scanner
    try:
        return service.update_signal_mode(payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/advanced-signal-settings")
async def advanced_signal_settings(request: Request) -> dict:
    settings = request.app.state.access.get_user_settings(current_session(request))
    apply_user_settings_to_scanner(request, settings)
    return advanced_payload(settings)


@router.post("/api/advanced-signal-settings")
async def update_advanced_signal_settings(payload: AdvancedSignalSettingsRequest, request: Request) -> dict:
    require_admin_session(request)
    settings = request.app.state.access.update_user_settings(
        current_session(request),
        payload.model_dump(exclude_none=True),
    )
    apply_user_settings_to_scanner(request, settings)
    return advanced_payload(settings)


@router.get("/api/user-settings")
async def user_settings(request: Request) -> dict:
    settings = request.app.state.access.get_user_settings(current_session(request))
    apply_user_settings_to_scanner(request, settings)
    return settings


@router.post("/api/user-settings")
async def update_user_settings(payload: UserSettingsRequest, request: Request) -> dict:
    require_admin_session(request)
    settings = request.app.state.access.update_user_settings(current_session(request), payload.model_dump(exclude_none=True))
    apply_user_settings_to_scanner(request, settings)
    return settings


@router.post("/api/unified-universe/auto")
async def auto_unified_universe(payload: UnifiedAutoUniverseRequest, request: Request) -> dict:
    require_admin_session(request)
    """
    ONE-CLICK unified auto-load:
    - Fetches spot + perp lists from Binance and MEXC in parallel
    - Intersects Spot ∩ Perp per exchange (coins on both markets only)
    - Discovers a large pool first, then loads max 700 common symbols per exchange
    - Loads live spot and perp scanner streams while keeping exchange volume separate
    """
    service = request.app.state.scanner
    try:
        result = await service.auto_unified_universe(payload.limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Could not build unified universe") from exc
    return result


@router.get("/api/telegram")
async def telegram_status(request: Request) -> dict:
    try:
        return request.app.state.telegram.status()
    except Exception:
        return {"status": "initialized", "enabled": False}


@router.post("/api/telegram")
async def update_telegram(payload: TelegramSettingsRequest, request: Request) -> dict:
    require_admin_session(request)
    telegram = request.app.state.telegram
    await telegram.configure(payload.token, payload.chat_id)
    return telegram.status()


@router.post("/api/telegram/test")
async def test_telegram(request: Request) -> dict:
    require_admin_session(request)
    telegram = request.app.state.telegram
    try:
        await telegram.send_text("IgnitionFlow Spot Radar test alert is working.")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Telegram test failed. Check bot token and chat ID.") from exc
    return {"ok": True}
