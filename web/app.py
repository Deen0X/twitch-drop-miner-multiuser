from __future__ import annotations

import sys
import os
import asyncio
import logging
import json
import re
import secrets
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).parent.parent))

from web.auth import UserManager
from web.multi_runner import MultiRunner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TwitchDrops.Web")

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

SECRET_KEY = DATA_DIR / ".secret_key"
def _get_secret() -> str:
    if SECRET_KEY.exists():
        return SECRET_KEY.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY.write_text(key)
    return key

app = FastAPI(title="Twitch Drops Miner")
app.add_middleware(SessionMiddleware, secret_key=_get_secret())
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

templates = Jinja2Templates(directory="web/templates")
static_path = Path(__file__).parent / "static"
static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

user_manager = UserManager()
multi_runner = MultiRunner.get_instance()

DEFAULT_ACCENT = "#9147ff"
_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _darken_hex(color: str, factor: float = 0.82) -> str:
    n = int(color[1:], 16)
    r = max(0, min(255, round(((n >> 16) & 0xFF) * factor)))
    g = max(0, min(255, round(((n >> 8) & 0xFF) * factor)))
    b = max(0, min(255, round((n & 0xFF) * factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _get_user_accent(user_id: int) -> str:
    data = _read_settings_data(user_id)
    color = data.get("accent_color")
    if isinstance(color, str) and _ACCENT_RE.match(color):
        return color
    return DEFAULT_ACCENT


def _read_settings_data(user_id: int) -> dict:
    try:
        settings_path = user_manager.ensure_user_dir(user_id) / "settings.json"
        if settings_path.exists():
            with open(settings_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_settings_data(user_id: int, data: dict) -> None:
    settings_path = user_manager.ensure_user_dir(user_id) / "settings.json"
    with open(settings_path, "w") as f:
        json.dump(data, f, indent=2)


def _accent_ctx(user_id: int) -> dict:
    accent = _get_user_accent(user_id)
    return {"accent": accent, "accent_2": _darken_hex(accent)}


def _history_path(user_id: int) -> Path:
    return user_manager.ensure_user_dir(user_id) / "history.json"


def _load_history(user_id: int) -> list:
    path = _history_path(user_id)
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _record_history(user_id: int) -> list:
    """Merge claimed drops from the miner inventory cache into history.json.

    The miner cache does not expose Twitch's claim timestamp, so we store the
    first time this web layer detects a drop as claimed.
    """
    new_entries: list = []
    try:
        user_dir = user_manager.ensure_user_dir(user_id)
        cache_file = user_dir / "inventory_cache.json"
        if not cache_file.exists():
            return new_entries
        with open(cache_file) as f:
            campaigns = json.load(f)
        if not isinstance(campaigns, list):
            return new_entries
        history = _load_history(user_id)
        seen = {h.get("key") for h in history if isinstance(h, dict)}
        now = datetime.now(timezone.utc).isoformat()
        changed = False
        for campaign in campaigns:
            if not isinstance(campaign, dict):
                continue
            game = campaign.get("game") or {}
            game_name = game.get("name") or ""
            for drop in campaign.get("drops") or []:
                if not isinstance(drop, dict) or not drop.get("is_claimed"):
                    continue
                key = f"{game.get('id', '')}:{campaign.get('id', '')}:{drop.get('id', '')}"
                if key in seen:
                    continue
                benefits = drop.get("benefit_images") or []
                history.append({
                    "key": key,
                    "game_id": game.get("id", ""),
                    "game": game_name,
                    "campaign_id": campaign.get("id", ""),
                    "campaign": campaign.get("name", ""),
                    "campaign_image": campaign.get("image_url", ""),
                    "drop_id": drop.get("id", ""),
                    "drop": drop.get("name", ""),
                    "image": benefits[0] if benefits else (drop.get("image_url") or campaign.get("image_url", "")),
                    "benefit_images": benefits,
                    "detected_at": now,
                    "campaign_ends_at": campaign.get("ends_at", ""),
                    "required_minutes": drop.get("required_minutes", 0),
                })
                seen.add(key)
                new_entries.append(history[-1])
                changed = True
        if changed:
            history.sort(key=lambda h: h.get("detected_at", ""), reverse=True)
            with open(user_dir / "history.json", "w") as f:
                json.dump(history, f, indent=2)
    except Exception as exc:
        logger.debug(f"History record failed for user {user_id}: {exc}")
    return new_entries


def _group_history(history: list) -> list:
    groups: dict[str, dict] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        name = item.get("game") or "Unknown game"
        group = groups.setdefault(name, {"game": name, "image": "", "items": []})
        if not group["image"]:
            group["image"] = item.get("campaign_image") or item.get("image") or ""
        group["items"].append(item)
    for group in groups.values():
        group["items"].sort(key=lambda h: h.get("detected_at", ""), reverse=True)
    return sorted(
        groups.values(),
        key=lambda g: (g["items"][0].get("detected_at", "") if g["items"] else ""),
        reverse=True,
    )


def _chart_last_days(entries: list, days: int = 14) -> dict:
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    counts: Counter = Counter()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("detected_at") or ""
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            counts[dt.date()] += 1
        except Exception:
            continue
    bars = []
    total = 0
    peak = 0
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        count = counts.get(day, 0)
        total += count
        peak = max(peak, count)
        bars.append({"label": day.strftime("%m-%d"), "count": count})
    width = 100.0 / max(len(bars), 1)
    for i, bar in enumerate(bars):
        bar["x"] = round(i * width + 0.6, 2)
        bar["w"] = round(max(width - 1.2, 0.6), 2)
        bar["h"] = round((bar["count"] / peak * 100.0) if peak else 0.0, 1)
    return {"bars": bars, "total": total, "peak": peak, "days": days}


def _inventory_summary(campaigns: list) -> dict:
    summary = {"active": 0, "upcoming": 0, "expired": 0, "finished": 0,
               "claimed_drops": 0, "total_drops": 0, "campaigns": 0}
    for campaign in campaigns or []:
        if not isinstance(campaign, dict):
            continue
        summary["campaigns"] += 1
        for key in ("active", "upcoming", "expired", "finished"):
            if campaign.get(key):
                summary[key] += 1
        for drop in campaign.get("drops") or []:
            summary["total_drops"] += 1
            if drop.get("is_claimed"):
                summary["claimed_drops"] += 1
    return summary


def progress_bar(pct: int | float, width: int = 10) -> str:
    value = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(value / 100.0 * width))
    return "▰" * filled + "▱" * (width - filled)


def _user_stats(user_id: int) -> dict:
    user_dir = user_manager.ensure_user_dir(user_id)
    _record_history(user_id)
    history = _load_history(user_id)
    chart = _chart_last_days(history)
    now = datetime.now(timezone.utc)
    last7 = 0
    for entry in history:
        if not isinstance(entry, dict):
            continue
        try:
            dt = datetime.fromisoformat(str(entry.get("detected_at", "")).replace("Z", "+00:00"))
            if (now - dt).days < 7:
                last7 += 1
        except Exception:
            pass
    games = len({e.get("game") for e in history if isinstance(e, dict) and e.get("game")})
    top_games = Counter(
        e.get("game") or "Unknown" for e in history if isinstance(e, dict)
    ).most_common(6)

    campaigns_data = []
    cache_file = user_dir / "inventory_cache.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                campaigns_data = json.load(f)
        except Exception:
            campaigns_data = []
    if not isinstance(campaigns_data, list):
        campaigns_data = []

    progress = {}
    progress_file = user_dir / "progress.json"
    if progress_file.exists():
        try:
            with open(progress_file) as f:
                progress = json.load(f)
        except Exception:
            progress = {}

    current_campaign = progress.get("current_campaign") or {}
    return {
        "chart": chart,
        "total": len(history),
        "last7": last7,
        "last14": chart["total"],
        "games": games,
        "top_games": top_games,
        "active_campaigns": sum(1 for c in campaigns_data if isinstance(c, dict) and c.get("active")),
        "inventory": _inventory_summary(campaigns_data),
        "current_game": current_campaign.get("game", ""),
        "current_campaign": current_campaign.get("name", ""),
    }


RUN_PORT = 4295
RUN_HOST = "0.0.0.0"
_update_job_task: asyncio.Task | None = None


async def _restart_process() -> None:
    """Restart the web app in place after an update (no Docker socket needed)."""
    await asyncio.sleep(2)
    try:
        await multi_runner.shutdown()
    except Exception:
        pass
    main_py = Path(__file__).parent.parent / "main.py"
    args = [sys.executable, str(main_py), "--web", "--port", str(RUN_PORT), "--host", RUN_HOST]
    logger.info("Restarting web process after update: %s", " ".join(args))
    try:
        os.chdir(str(main_py.parent))
        os.execv(sys.executable, args)
    except Exception as exc:
        logger.exception("Self-restart failed: %s", exc)

SESSION_COOKIE = "tdm_session"
SESSION_EXPIRY = timedelta(hours=24)


def _get_session(request: Request) -> Optional[dict]:
    data = request.session.get(SESSION_COOKIE)
    if data is None:
        return None
    expires = datetime.fromisoformat(data.get("expires", ""))
    if expires < datetime.now(timezone.utc):
        del request.session[SESSION_COOKIE]
        return None
    return data


def _require_auth(request: Request) -> dict:
    session = _get_session(request)
    if session is None:
        raise HTTPException(401, "Not authenticated")
    return session


async def _start_user_instance(user_id: int):
    user_dir = user_manager.ensure_user_dir(user_id)
    await multi_runner.start_instance(user_id, user_dir)


@app.get("/")
async def root(request: Request):
    session = _get_session(request)
    if session is None:
        return RedirectResponse("/login")
    user = user_manager.get_user(session["user_id"])
    if user is None:
        return RedirectResponse("/logout")
    if user["is_admin"]:
        from starlette.responses import RedirectResponse as StarletRedirectResponse
        return StarletRedirectResponse("/admin", status_code=303)
    return RedirectResponse("/dashboard")


@app.get("/login")
async def login_page(request: Request):
    session = _get_session(request)
    if session:
        user = user_manager.get_user(session["user_id"])
        if user and user["is_admin"]:
            from starlette.responses import RedirectResponse as StarletRedirectResponse
            return StarletRedirectResponse("/admin", status_code=303)
        from starlette.responses import RedirectResponse as StarletRedirectResponse
        return StarletRedirectResponse("/dashboard", status_code=303)
    tmpl = templates.env.get_template("login.html")
    ctx = {
        "request": request,
        "error": None,
        "accent": DEFAULT_ACCENT,
        "accent_2": _darken_hex(DEFAULT_ACCENT),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    form_data = await request.form()
    remember = form_data.get("remember", "off") == "on"
    user = user_manager.verify_user(username, password)
    if user is None:
        tmpl = templates.env.get_template("login.html")
        ctx = {
            "request": request,
            "error": "Invalid username or password",
            "accent": DEFAULT_ACCENT,
            "accent_2": _darken_hex(DEFAULT_ACCENT),
        }
        return Response(tmpl.render(ctx), media_type="text/html")
    expires_delta = timedelta(days=7) if remember else SESSION_EXPIRY
    expires = (datetime.now(timezone.utc) + expires_delta).isoformat()
    request.session[SESSION_COOKIE] = {
        "user_id": user["id"],
        "username": user["username"],
        "is_admin": bool(user["is_admin"]),
        "expires": expires
    }
    if user["is_admin"]:
        from starlette.responses import RedirectResponse as StarletRedirectResponse
        return StarletRedirectResponse("/admin", status_code=303)
    from starlette.responses import RedirectResponse as StarletRedirectResponse
    return StarletRedirectResponse("/dashboard", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
async def logout_get(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard")
async def dashboard(request: Request):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    snapshot = multi_runner.get_instance_snapshot(session["user_id"])
    user_dir = user_manager.get_user_dir(session["user_id"])
    
    # Enrich snapshot with real campaign data from progress.json
    progress_file = user_dir / "progress.json"
    if progress_file.exists():
        try:
            with open(progress_file) as f:
                progress_data = json.load(f)
            if progress_data.get("current_campaign"):
                snapshot["current_game"] = progress_data["current_campaign"].get("game", "")
                snapshot["campaign_name"] = progress_data["current_campaign"].get("name", "")
        except Exception:
            pass
    
    # Get active campaigns count from inventory cache
    cache_file = user_dir / "inventory_cache.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                campaigns = json.load(f)
            snapshot["campaigns_count"] = sum(1 for c in campaigns if (c.get("active") or c.get("upcoming")) and not c.get("finished") and not c.get("expired"))
        except Exception:
            pass
    
    settings_path = user_dir / "settings.json"
    user_settings = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                user_settings = json.load(f)
        except (json.JSONDecodeError, ValueError):
            user_settings = {}
    _record_history(session["user_id"])
    history = _load_history(session["user_id"])
    tmpl = templates.env.get_template("dashboard.html")
    ctx = {
        "request": request,
        "user": user,
        "snapshot": snapshot,
        "settings": user_settings,
        "is_admin": session.get("is_admin", False),
        "active": "dashboard",
        "chart": _chart_last_days(history),
        "claimed_total": len(history),
        **_accent_ctx(session["user_id"]),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/inventory")
async def inventory_page(request: Request):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    tmpl = templates.env.get_template("inventory.html")
    ctx = {
        "request": request,
        "user": user,
        "is_admin": session.get("is_admin", False),
        "active": "inventory",
        **_accent_ctx(session["user_id"]),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/history")
async def history_page(request: Request):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    _record_history(session["user_id"])
    history = _load_history(session["user_id"])
    groups = _group_history(history)
    tmpl = templates.env.get_template("history.html")
    ctx = {
        "request": request,
        "user": user,
        "is_admin": session.get("is_admin", False),
        "active": "history",
        "groups": groups,
        "total": len(history),
        "chart": _chart_last_days(history),
        **_accent_ctx(session["user_id"]),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/stats")
async def stats_page(request: Request):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    stats = _user_stats(session["user_id"])
    tmpl = templates.env.get_template("stats.html")
    ctx = {
        "request": request,
        "user": user,
        "is_admin": session.get("is_admin", False),
        "active": "stats",
        "stats": stats,
        **_accent_ctx(session["user_id"]),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/telegram")
async def telegram_page(request: Request):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    settings = _read_settings_data(session["user_id"])
    from web.telegram_bot import get_config
    bot_username = get_config().get("bot_username", "")
    tg_user = {
        "username": settings.get("telegram_username", ""),
        "user_id": settings.get("telegram_user_id", ""),
        "chat_linked": bool(settings.get("telegram_chat_id")),
        "notifications": settings.get("telegram_notifications", True),
        "notify_current": settings.get("telegram_notify_current", True),
        "notify_upcoming": settings.get("telegram_notify_upcoming", False),
    }
    tg_admin = None
    if session.get("is_admin"):
        from web.telegram_bot import get_config, get_token
        cfg = get_config()
        linked = []
        for item in user_manager.list_users(include_inactive=True):
            data = _read_settings_data(item["id"])
            if data.get("telegram_username") or data.get("telegram_user_id") or data.get("telegram_chat_id"):
                linked.append({
                    "display_name": item.get("display_name") or item["username"],
                    "username": item["username"],
                    "telegram_username": data.get("telegram_username", ""),
                    "telegram_user_id": data.get("telegram_user_id", ""),
                    "chat_linked": bool(data.get("telegram_chat_id")),
                    "notifications": data.get("telegram_notifications", True),
                })
        tg_admin = {
            "configured": bool(get_token()),
            "bot_username": cfg.get("bot_username", ""),
            "enabled": cfg.get("enabled", False),
            "drop_message": cfg.get("drop_message", ""),
            "running": bool(_telegram_bot and _telegram_bot.running),
            "linked": linked,
        }
    tmpl = templates.env.get_template("telegram.html")
    ctx = {
        "request": request,
        "user": user,
        "is_admin": session.get("is_admin", False),
        "active": "telegram",
        "tg_user": tg_user,
        "tg_admin": tg_admin,
        "tg_bot_username": bot_username,
        **_accent_ctx(session["user_id"]),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/api/telegram/settings")
async def api_telegram_get(request: Request):
    session = _require_auth(request)
    settings = _read_settings_data(session["user_id"])
    return {
        "username": settings.get("telegram_username", ""),
        "user_id": settings.get("telegram_user_id", ""),
        "chat_linked": bool(settings.get("telegram_chat_id")),
        "notifications": settings.get("telegram_notifications", True),
        "notify_current": settings.get("telegram_notify_current", True),
        "notify_upcoming": settings.get("telegram_notify_upcoming", False),
    }


@app.post("/api/telegram/settings")
async def api_telegram_set(request: Request):
    session = _require_auth(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    username = str(data.get("username") or "").strip().lstrip("@")
    user_id = str(data.get("user_id") or "").strip()
    notifications = bool(data.get("notifications", True))
    if username and not re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
        raise HTTPException(400, "Invalid Telegram username")
    if user_id and not user_id.isdigit():
        raise HTTPException(400, "Telegram user ID must be numeric")

    for item in user_manager.list_users(include_inactive=True):
        if item["id"] == session["user_id"]:
            continue
        other = _read_settings_data(item["id"])
        if username and str(other.get("telegram_username", "")).lstrip("@").lower() == username.lower():
            raise HTTPException(400, "That Telegram username is already linked to another user")
        if user_id and str(other.get("telegram_user_id", "")) == user_id:
            raise HTTPException(400, "That Telegram user ID is already linked to another user")

    current = _read_settings_data(session["user_id"])
    identity_changed = (
        str(current.get("telegram_username", "")).lstrip("@").lower() != username.lower()
        or str(current.get("telegram_user_id", "")) != user_id
    )
    current["telegram_username"] = username
    current["telegram_user_id"] = user_id
    current["telegram_notifications"] = notifications
    current["telegram_notify_current"] = bool(
        data.get("notify_current", current.get("telegram_notify_current", True))
    )
    current["telegram_notify_upcoming"] = bool(
        data.get("notify_upcoming", current.get("telegram_notify_upcoming", False))
    )
    if identity_changed:
        current.pop("telegram_chat_id", None)
    _write_settings_data(session["user_id"], current)
    return {
        "success": True,
        "chat_linked": bool(current.get("telegram_chat_id")),
        "identity_changed": identity_changed,
        "notify_current": current["telegram_notify_current"],
        "notify_upcoming": current["telegram_notify_upcoming"],
    }


@app.post("/api/telegram/unlink")
async def api_telegram_unlink(request: Request):
    session = _require_auth(request)
    current = _read_settings_data(session["user_id"])
    current.pop("telegram_chat_id", None)
    _write_settings_data(session["user_id"], current)
    return {"success": True}


@app.get("/api/admin/telegram/config")
async def api_admin_telegram_config(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    from web.telegram_bot import get_config, get_token
    cfg = get_config()
    linked = []
    for item in user_manager.list_users(include_inactive=True):
        data = _read_settings_data(item["id"])
        if data.get("telegram_username") or data.get("telegram_user_id") or data.get("telegram_chat_id"):
            linked.append({
                "display_name": item.get("display_name") or item["username"],
                "username": item["username"],
                "telegram_username": data.get("telegram_username", ""),
                "telegram_user_id": data.get("telegram_user_id", ""),
                "chat_linked": bool(data.get("telegram_chat_id")),
                "notifications": data.get("telegram_notifications", True),
            })
    return {
        "configured": bool(get_token()),
        "bot_username": cfg.get("bot_username", ""),
        "enabled": cfg.get("enabled", False),
        "drop_message": cfg.get("drop_message", ""),
        "running": bool(_telegram_bot and _telegram_bot.running),
        "linked": linked,
    }


@app.post("/api/admin/telegram/config")
async def api_admin_telegram_config_set(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    from web.telegram_bot import TelegramBot, get_config, get_token, set_config, set_token
    token = str(data.get("token") or "").strip()
    if token:
        probe = TelegramBot(user_manager)
        probe.token = token
        me = await probe.get_me()
        if not me:
            raise HTTPException(400, "Invalid Telegram bot token")
        set_token(token)
        set_config(bot_username=me.get("username", ""))
    cfg = set_config(
        enabled=bool(data.get("enabled", False)),
        drop_message=str(data.get("drop_message") or "").strip(),
    )
    await _start_telegram_bot()
    return {
        "success": True,
        "configured": bool(get_token()),
        "bot_username": cfg.get("bot_username", ""),
        "enabled": cfg.get("enabled", False),
        "drop_message": cfg.get("drop_message", ""),
        "running": bool(_telegram_bot and _telegram_bot.running),
    }


@app.post("/api/admin/telegram/test")
async def api_admin_telegram_test(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    settings = _read_settings_data(session["user_id"])
    chat_id = settings.get("telegram_chat_id")
    if not chat_id:
        raise HTTPException(400, "Link your own Telegram account first")
    if _telegram_bot is None:
        raise HTTPException(400, "Bot is not running")
    await _telegram_bot.send_message(int(chat_id), "<b>Test</b> from Twitch Drop Miner ✅")
    return {"success": True}


@app.get("/api/status")
async def api_status(request: Request):
    session = _require_auth(request)
    snapshot = multi_runner.get_instance_snapshot(session["user_id"])
    return snapshot


@app.get("/api/inventory")
async def api_inventory(request: Request):
    session = _require_auth(request)
    _record_history(session["user_id"])
    user_dir = user_manager.get_user_dir(session["user_id"])
    cache_file = user_dir / "inventory_cache.json"

    if cache_file.exists():
        try:
            with open(cache_file) as f:
                return json.load(f)
        except Exception:
            pass

    return []


@app.get("/api/progress")
async def api_progress(request: Request):
    session = _require_auth(request)
    user_dir = user_manager.get_user_dir(session["user_id"])
    progress_file = user_dir / "progress.json"
    cache_file = user_dir / "inventory_cache.json"
    
    instance_status = multi_runner.get_instance_snapshot(session["user_id"])
    is_running = instance_status.get("running", False)
    
    has_campaigns_to_mine = False
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                campaigns = json.load(f)
            for c in campaigns:
                if (c.get("active") or c.get("upcoming")) and not c.get("finished") and not c.get("expired"):
                    has_campaigns_to_mine = True
                    break
        except Exception:
            pass
    
    is_mining = False
    if progress_file.exists():
        try:
            with open(progress_file) as f:
                data = json.load(f)
            drops = data.get("drops", {})
            for drop_id, drop_info in drops.items():
                if drop_info.get("current_minutes", 0) > 0:
                    is_mining = True
                    break
        except Exception:
            pass
    
    is_active = is_mining
    
    if not progress_file.exists():
        return {"active": is_active, "drops": {}, "mining": is_running, "waiting_campaigns": not has_campaigns_to_mine}
    
    try:
        with open(progress_file) as f:
            data = json.load(f)
        return {
            "active": is_active,
            "drops": data.get("drops", {}),
            "mining": is_running,
            "currently_mining": is_mining,
            "waiting_campaigns": not has_campaigns_to_mine,
            "current_campaign": data.get("current_campaign")
        }
    except Exception:
        return {"active": is_active, "drops": {}, "mining": is_running, "waiting_campaigns": not has_campaigns_to_mine}


@app.post("/api/settings")
async def api_settings(request: Request):
    session = _require_auth(request)
    try:
        settings = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    priority = settings.get("priority", [])
    exclude = settings.get("exclude", [])
    lang = settings.get("language", "English")
    dark_mode = settings.get("dark_mode", False)
    inv_view_mode = settings.get("inventory_view_mode", "my")
    inv_filters = settings.get("inventory_filters", {
        "active": False, "upcoming": True, "expired": False,
        "not_linked": False, "excluded": False, "finished": False
    })
    user_dir = user_manager.ensure_user_dir(session["user_id"])
    settings_path = user_dir / "settings.json"
    current = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                current = json.load(f)
        except (json.JSONDecodeError, ValueError):
            current = {}
    if "priority" in settings:
        current["priority"] = settings["priority"]
    if "exclude" in settings:
        current["exclude"] = settings["exclude"]
    if "language" in settings:
        current["language"] = settings["language"]
    if "dark_mode" in settings:
        current["dark_mode"] = settings["dark_mode"]
    if "inventory_view_mode" in settings:
        current["inventory_view_mode"] = settings["inventory_view_mode"]
    if "inventory_filters" in settings:
        current["inventory_filters"] = settings["inventory_filters"]
    if "inventory_only_mine" in settings:
        current["inventory_only_mine"] = bool(settings["inventory_only_mine"])
    with open(settings_path, "w") as f:
        json.dump(current, f, indent=2)

    cache_file = user_dir / "inventory_cache.json"
    if cache_file.exists():
        cache_file.unlink()

    return {"success": True}


@app.post("/api/accent")
async def api_set_accent(request: Request):
    session = _require_auth(request)
    try:
        data = await request.json()
        accent = str(data.get("accent", "")).strip()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if not _ACCENT_RE.match(accent):
        raise HTTPException(400, "Invalid accent color (expected #RRGGBB)")

    user_dir = user_manager.ensure_user_dir(session["user_id"])
    settings_path = user_dir / "settings.json"
    current = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                current = json.load(f)
        except (json.JSONDecodeError, ValueError):
            current = {}
    current["accent_color"] = accent
    with open(settings_path, "w") as f:
        json.dump(current, f, indent=2)
    return {"success": True, "accent": accent}


@app.get("/api/settings")
async def api_get_settings(request: Request):
    session = _require_auth(request)
    user_dir = user_manager.ensure_user_dir(session["user_id"])
    settings_path = user_dir / "settings.json"

    settings_data = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings_data = json.load(f)
        except Exception:
            pass

    inv_filters = settings_data.get("inventory_filters", {
        "active": False, "upcoming": True, "expired": False,
        "not_linked": False, "excluded": False, "finished": False
    })

    return {
        "priority": settings_data.get("priority", []),
        "exclude": settings_data.get("exclude", []),
        "language": settings_data.get("language", "English"),
        "dark_mode": settings_data.get("dark_mode", False),
        "inventory_view_mode": settings_data.get("inventory_view_mode", "my"),
        "inventory_filters": inv_filters,
        "accent_color": settings_data.get("accent_color", DEFAULT_ACCENT),
        "inventory_only_mine": settings_data.get("inventory_only_mine", True),
    }


@app.post("/api/priority/add")
async def api_priority_add(request: Request):
    session = _require_auth(request)
    try:
        data = await request.json()
        game_id = data.get("game_id")
        game_name = data.get("game_name")
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if not game_id or not game_name:
        raise HTTPException(400, "game_id and game_name required")

    user_dir = user_manager.ensure_user_dir(session["user_id"])
    settings_path = user_dir / "settings.json"

    settings_data = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings_data = json.load(f)
        except Exception:
            pass

    priority = settings_data.get("priority", [])
    if not isinstance(priority, list):
        priority = []
    game_name_str = str(game_name).strip()
    if game_name_str and game_name_str not in priority:
        priority.append(game_name_str)
        settings_data["priority"] = priority
        with open(settings_path, 'w') as f:
            json.dump(settings_data, f, indent=2)

        cache_file = user_dir / "inventory_cache.json"
        if cache_file.exists():
            cache_file.unlink()

    return {"success": True, "priority": priority}


@app.post("/api/exclude/add")
async def api_exclude_add(request: Request):
    session = _require_auth(request)
    try:
        data = await request.json()
        game_id = data.get("game_id")
        game_name = data.get("game_name")
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if not game_id or not game_name:
        raise HTTPException(400, "game_id and game_name required")

    user_dir = user_manager.ensure_user_dir(session["user_id"])
    settings_path = user_dir / "settings.json"

    settings_data = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings_data = json.load(f)
        except Exception:
            pass

    exclude = settings_data.get("exclude", [])
    if not isinstance(exclude, list):
        exclude = []
    game_name_str = str(game_name).strip()
    if game_name_str and game_name_str not in exclude:
        exclude.append(game_name_str)
        settings_data["exclude"] = exclude
        with open(settings_path, 'w') as f:
            json.dump(settings_data, f, indent=2)

        cache_file = user_dir / "inventory_cache.json"
        if cache_file.exists():
            cache_file.unlink()

    return {"success": True, "exclude": exclude}


@app.post("/api/priority/remove")
async def api_priority_remove(request: Request):
    session = _require_auth(request)
    try:
        data = await request.json()
        game_id = data.get("game_id")
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    if not game_id:
        raise HTTPException(400, "game_id required")

    user_dir = user_manager.ensure_user_dir(session["user_id"])
    settings_path = user_dir / "settings.json"

    settings_data = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings_data = json.load(f)
        except Exception:
            pass

    priority = settings_data.get("priority", [])
    priority = [p for p in priority if p.get("id") != game_id]
    settings_data["priority"] = priority
    with open(settings_path, 'w') as f:
        json.dump(settings_data, f, indent=2)

    return {"success": True, "priority": priority}


@app.post("/api/kill")
async def api_kill(request: Request):
    session = _require_auth(request)
    logger.info(f"api_kill called, user_id={session['user_id']}, instances={list(multi_runner._instances.keys())}")
    
    user_id = session["user_id"]
    if user_id in multi_runner._instances:
        inst = multi_runner._instances[user_id]
        logger.info(f"api_kill found instance for user {user_id}")
        # Fire and forget - don't wait for stop to complete
        asyncio.create_task(inst.stop())
        del multi_runner._instances[user_id]
    
    return {"success": True}


@app.post("/api/restart")
async def api_restart(request: Request):
    session = _require_auth(request)
    user_dir = user_manager.ensure_user_dir(session["user_id"])
    cookies_path = user_dir / "cookies.jar"
    if not cookies_path.exists():
        return {"success": False, "error": "No Twitch account linked"}
    
    user_id = session["user_id"]
    
    # Kill any existing instance first, then start new one
    if user_id in multi_runner._instances:
        inst = multi_runner._instances[user_id]
        await inst.stop()  # This will terminate/kill the subprocess
        del multi_runner._instances[user_id]
    
    await multi_runner.start_instance(user_id, user_dir)
    return {"success": True}
    return {"success": True}


@app.post("/api/change-password")
async def api_change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...)):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    if not user_manager._verify_password(current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    try:
        user_manager.change_password(session["user_id"], new_password)
        return {"success": True, "message": "Password changed successfully"}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/change-display-name")
async def api_change_display_name(request: Request, display_name: str = Form(...)):
    session = _require_auth(request)
    if len(display_name) < 1 or len(display_name) > 32:
        raise HTTPException(400, "Display name must be 1-32 characters")
    user_manager.update_user(session["user_id"], display_name=display_name)
    return {"success": True}


@app.get("/profile")
async def profile_page(request: Request):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    tmpl = templates.env.get_template("profile.html")
    ctx = {
        "request": request,
        "user": user,
        "is_admin": session.get("is_admin", False),
        "active": "profile",
        **_accent_ctx(session["user_id"]),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/admin")
async def admin_page(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    user = user_manager.get_user(session["user_id"])
    users = user_manager.list_users(include_inactive=True)
    all_status = multi_runner.get_all_snapshots()
    all_history = []
    for item in users:
        _record_history(item["id"])
        all_history.extend(_load_history(item["id"]))
    top_games = Counter(
        entry.get("game") or "Unknown"
        for entry in all_history if isinstance(entry, dict)
    ).most_common(6)
    running_count = sum(1 for snapshot in all_status.values() if snapshot.get("running"))
    tmpl = templates.env.get_template("admin.html")
    ctx = {
        "request": request,
        "user": user,
        "users": users,
        "status": all_status,
        "is_admin": True,
        "active": "admin",
        "chart": _chart_last_days(all_history),
        "top_games": top_games,
        "running_count": running_count,
        "claimed_total": len(all_history),
        "user_count": len(users),
        **_accent_ctx(session["user_id"]),
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/api/generate-code")
async def api_generate_code(request: Request):
    session = _require_auth(request)
    try:
        from web.device_code_manager import DeviceCodeManager
        dm = DeviceCodeManager.get_instance()
        info = await dm.generate_code(session["user_id"])
        return {
            "success": True,
            "user_code": info.user_code,
            "verification_uri": info.verification_uri
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/code-status")
async def api_code_status(request: Request):
    session = _require_auth(request)
    user_dir = user_manager.get_user_dir(session["user_id"])
    cookies_path = user_dir / "cookies.jar"
    if cookies_path.exists():
        return {"success": True, "status": "linked", "message": "Account is linked"}
    from web.device_code_manager import DeviceCodeManager
    dm = DeviceCodeManager.get_instance()
    info = dm.get_code_any_status(session["user_id"])
    if info is None:
        return {"success": True, "status": "not_generated", "message": "No code generated yet"}
    if info.status == "completed":
        return {"success": True, "status": "linked", "message": "Account linked successfully"}
    elif info.status == "failed":
        return {"success": True, "status": "failed", "message": info.error or "Code activation failed"}
    return {"success": True, "status": "pending", "message": "Code entered, waiting for Twitch confirmation..."}


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    users = user_manager.list_users(include_inactive=True)
    all_status = multi_runner.get_all_snapshots()
    return {"users": users, "status": all_status}


@app.get("/api/admin/status")
async def api_admin_status(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    return multi_runner.get_all_snapshots()


@app.post("/api/admin/users")
async def api_admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form("")
):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    try:
        user_id = user_manager.create_user(username, password, display_name)
        return {"success": True, "user_id": user_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/users/{user_id}/toggle")
async def api_admin_toggle_user(request: Request, user_id: int):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    user = user_manager.get_user(user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    new_state = not user["is_active"]
    user_manager.update_user(user_id, is_active=new_state)
    if not new_state:
        await multi_runner.stop_instance(user_id)
    return {"success": True, "is_active": new_state}


@app.post("/api/admin/users/{user_id}/generate-code")
async def api_admin_generate_code(request: Request, user_id: int):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    user = user_manager.get_user(user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    try:
        import aiohttp
        client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
        headers = {"Client-ID": client_id, "Content-Type": "application/x-www-form-urlencoded"}
        payload = {"client_id": client_id, "scopes": "user:read:email"}
        async with aiohttp.ClientSession() as http:
            async with http.post("https://id.twitch.tv/oauth2/device", headers=headers, data=payload) as resp:
                data = await resp.json()
                if resp.status != 200:
                    return {"success": False, "error": data.get("message", "Failed to generate code")}
                user_code = data.get("user_code", "")
                verification_uri = data.get("verification_uri", "https://www.twitch.tv/activate")
                return {
                    "success": True,
                    "user_code": user_code,
                    "verification_uri": verification_uri
                }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.delete("/api/admin/users/{user_id}")
async def api_admin_delete_user(request: Request, user_id: int):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    if user_id == session["user_id"]:
        raise HTTPException(400, "Cannot delete yourself")
    user_manager.delete_user(user_id)
    await multi_runner.stop_instance(user_id)
    return {"success": True}


@app.post("/api/admin/users/{user_id}/start")
async def api_admin_start_user(request: Request, user_id: int):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    user = user_manager.get_user(user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user_dir = user_manager.ensure_user_dir(user_id)
    cookies_path = user_dir / "cookies.jar"
    if not cookies_path.exists():
        return {"success": False, "error": "No Twitch account linked. Please link account first."}
    await multi_runner.start_instance(user_id, user_dir)
    return {"success": True}


@app.post("/api/admin/users/{user_id}/stop")
async def api_admin_stop_user(request: Request, user_id: int):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    await multi_runner.stop_instance(user_id)
    return {"success": True}


@app.post("/api/admin/users/{user_id}/change-password")
async def api_admin_change_password(request: Request, user_id: int):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    try:
        data = await request.form()
        new_password = data.get("new_password")
    except Exception:
        raise HTTPException(400, "Invalid request")
    if not new_password or len(new_password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")
    try:
        user_manager.change_password(user_id, new_password)
        return {"success": True}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/admin/users/{user_id}/overview")
async def api_admin_user_overview(request: Request, user_id: int):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    user = user_manager.get_user(user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user_dir = user_manager.ensure_user_dir(user_id)
    _record_history(user_id)
    snapshot = multi_runner.get_instance_snapshot(user_id)

    progress = {}
    progress_file = user_dir / "progress.json"
    if progress_file.exists():
        try:
            with open(progress_file) as f:
                progress = json.load(f)
        except Exception:
            progress = {}

    campaigns = []
    cache_file = user_dir / "inventory_cache.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                campaigns = json.load(f)
        except Exception:
            campaigns = []
    inventory = _inventory_summary(campaigns if isinstance(campaigns, list) else [])

    history = _load_history(user_id)
    settings_data = {}
    settings_file = user_dir / "settings.json"
    if settings_file.exists():
        try:
            with open(settings_file) as f:
                settings_data = json.load(f)
        except Exception:
            settings_data = {}

    return {
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "is_admin": bool(user.get("is_admin")),
            "is_active": bool(user.get("is_active", 1)),
        },
        "snapshot": snapshot,
        "progress": progress,
        "inventory": inventory,
        "history": history[:50],
        "history_total": len(history),
        "chart": _chart_last_days(history),
        "settings": {
            "priority": settings_data.get("priority", []),
            "exclude": settings_data.get("exclude", []),
            "only_mine": settings_data.get("inventory_only_mine", True),
            "accent": settings_data.get("accent_color", DEFAULT_ACCENT),
        },
    }


@app.get("/api/admin/check-update")
async def api_check_update(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    from web.github_update import check_updates
    return await check_updates()


async def _run_update_job() -> None:
    from web.github_update import apply_update
    result = await apply_update(dry_run=False)
    if result.get("success") and not result.get("up_to_date"):
        asyncio.create_task(_restart_process())


@app.post("/api/admin/apply-update")
async def api_apply_update(request: Request):
    global _update_job_task
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    from web.github_update import get_progress
    if get_progress().get("active"):
        return {"started": False, "message": "An update is already in progress"}
    _update_job_task = asyncio.create_task(_run_update_job())
    return {"started": True, "message": "Update started"}


@app.get("/api/admin/update-progress")
async def api_update_progress(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    from web.github_update import get_progress
    return get_progress()


@app.get("/api/admin/update-config")
async def api_get_update_config(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    from web.github_update import get_update_config
    return get_update_config()


@app.post("/api/admin/update-config")
async def api_set_update_config(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    try:
        hour = int(data.get("hour", 4)) % 24
    except Exception:
        hour = 4
    from web.github_update import set_update_config
    return set_update_config(enabled=bool(data.get("enabled", False)), hour=hour)


async def start_runner():
    asyncio.create_task(multi_runner.start_monitoring())
    all_users = user_manager.list_users()
    for user in all_users:
        user_dir = user_manager.ensure_user_dir(user["id"])
        cookies_path = user_dir / "cookies.jar"
        if cookies_path.exists():
            await multi_runner.start_instance(user["id"], user_dir)


_history_task: asyncio.Task | None = None
_auto_update_task: asyncio.Task | None = None
_telegram_bot = None


async def _history_watcher() -> None:
    while True:
        try:
            for user in user_manager.list_users(include_inactive=False):
                hist_path = user_manager.ensure_user_dir(user["id"]) / "history.json"
                existed = hist_path.exists()
                new_entries = _record_history(user["id"])
                if existed and new_entries and _telegram_bot is not None:
                    for entry in new_entries:
                        try:
                            await _telegram_bot.notify_drop(user["id"], entry)
                        except Exception as exc:
                            logger.debug("Telegram notify failed: %s", exc)
                if _telegram_bot is not None:
                    try:
                        await _telegram_bot.notify_upcoming(user["id"])
                    except Exception as exc:
                        logger.debug("Telegram upcoming notify failed: %s", exc)
        except Exception as exc:
            logger.debug(f"History watcher error: {exc}")
        await asyncio.sleep(120)


async def _start_telegram_bot() -> None:
    global _telegram_bot
    from web.telegram_bot import TelegramBot, get_config, get_token
    if _telegram_bot is not None:
        try:
            await _telegram_bot.stop()
        except Exception:
            pass
        _telegram_bot = None
    cfg = get_config()
    if cfg.get("enabled") and get_token():
        bot = TelegramBot(user_manager)
        if await bot.start():
            _telegram_bot = bot


async def _auto_update_loop() -> None:
    from web.github_update import get_update_config, set_update_config, check_updates, apply_update
    while True:
        try:
            cfg = get_update_config()
            if cfg.get("enabled"):
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.hour >= int(cfg.get("hour", 4)) and cfg.get("last_check") != today:
                    set_update_config(last_check=today, last_result="checking")
                    res = await check_updates()
                    upstream = res.get("upstream") or {}
                    ours = res.get("ours") or {}
                    if upstream.get("update_available") or ours.get("update_available"):
                        set_update_config(last_result="updating")
                        out = await apply_update(dry_run=False)
                        set_update_config(last_result=(out.get("message") or "done"))
                        if out.get("success") and not out.get("up_to_date"):
                            asyncio.create_task(_restart_process())
                            return
                    else:
                        set_update_config(last_result="up to date")
        except Exception as exc:
            logger.debug(f"Auto-update error: {exc}")
        await asyncio.sleep(600)


@app.on_event("startup")
async def startup():
    global _history_task, _auto_update_task
    await start_runner()
    _history_task = asyncio.create_task(_history_watcher())
    _auto_update_task = asyncio.create_task(_auto_update_loop())
    await _start_telegram_bot()


@app.on_event("shutdown")
async def shutdown():
    global _history_task, _auto_update_task, _telegram_bot
    for task in (_history_task, _auto_update_task):
        if task is not None:
            task.cancel()
    _history_task = None
    _auto_update_task = None
    if _telegram_bot is not None:
        try:
            await _telegram_bot.stop()
        except Exception:
            pass
        _telegram_bot = None
    await multi_runner.shutdown()


def run(port: int = 4295, host: str = "0.0.0.0"):
    global RUN_PORT, RUN_HOST
    RUN_PORT, RUN_HOST = port, host
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Twitch Drops Miner Web Server")
    parser.add_argument("--port", type=int, default=4295)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    run(port=args.port, host=args.host)