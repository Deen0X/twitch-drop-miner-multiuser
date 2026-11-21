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

from fastapi import FastAPI, Request, Form, HTTPException, Response, UploadFile, File
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


async def _reload_miner_settings(user_id: int) -> bool:
    """Mining settings are read at startup: restart the miner to apply them."""
    user_dir = user_manager.ensure_user_dir(user_id)
    if not (user_dir / "cookies.jar").exists():
        return False
    snapshot = multi_runner.get_instance_snapshot(user_id)
    if not snapshot.get("running"):
        return False
    await multi_runner.stop_instance(user_id)
    await multi_runner.start_instance(user_id, user_dir)
    logger.info("Restarted miner for user %s after a settings change", user_id)
    return True


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
    if "priority_mode" in settings:
        current["priority_mode"] = settings["priority_mode"]
    with open(settings_path, "w") as f:
        json.dump(current, f, indent=2)

    cache_file = user_dir / "inventory_cache.json"
    if cache_file.exists():
        cache_file.unlink()

    if any(k in settings for k in ("priority", "exclude", "priority_mode")):
        await _reload_miner_settings(session["user_id"])

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

        await _reload_miner_settings(session["user_id"])

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

        await _reload_miner_settings(session["user_id"])

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
    if not isinstance(priority, list):
        priority = []
    priority = [
        p for p in priority
        if (p.get("id") if isinstance(p, dict) else p) != game_id
        and (p.get("name") if isinstance(p, dict) else p) != game_id
    ]
    settings_data["priority"] = priority
    with open(settings_path, 'w') as f:
        json.dump(settings_data, f, indent=2)

    await _reload_miner_settings(session["user_id"])

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


async def _revoke_twitch_token(cookies_path: Path) -> bool:
    """Best-effort token revocation, mirroring upstream's 'Invalidate' button."""
    try:
        import aiohttp
        from yarl import URL
        from web.device_code_manager import DEVICE_CLIENT_ID, LEGACY_CLIENT_ID

        jar = aiohttp.CookieJar()
        jar.load(cookies_path)
        cookies = jar.filter_cookies(URL("https://www.twitch.tv"))
        morsel = cookies.get("auth-token")
        token = getattr(morsel, "value", "") or ""
    except Exception as exc:
        logger.debug("Could not read Twitch token: %s", exc)
        return False
    if not token:
        return False
    # Tokens linked from the browser use the web client; older ones the Android
    # app, and the device flow the mobile web client. Try each id.
    for client_id in (DEVICE_CLIENT_ID, LEGACY_CLIENT_ID, "kimne78kx3ncx6brgo4mv6wki5h1ko"):
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    "https://id.twitch.tv/oauth2/revoke",
                    headers={
                        "Client-ID": client_id,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"client_id": client_id, "token": token},
                ) as resp:
                    if resp.status == 200:
                        return True
        except Exception as exc:
            logger.warning("Twitch token revoke error: %s", exc)
    logger.warning("Twitch token revoke failed for both clients")
    return False


async def _validate_twitch_token(token: str) -> Optional[dict]:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as http:
            async with http.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {token}"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except Exception as exc:
        logger.debug("Twitch token validation failed: %s", exc)
        return None


async def _gql_can_mine(token: str, client_id: str) -> tuple[bool, str]:
    """Run the real campaign query: catches 401 and IntegrityCheckFailed."""
    try:
        import aiohttp
        from constants import GQL_QUERIES
        async with aiohttp.ClientSession() as http:
            async with http.post(
                "https://gql.twitch.tv/gql",
                json=GQL_QUERIES["Campaigns"],
                headers={"Client-ID": client_id, "Authorization": f"OAuth {token}"},
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status != 200:
                    return False, f"Twitch GQL returned HTTP {resp.status}"
                data = await resp.json(content_type=None)
    except Exception as exc:
        return False, f"Twitch GQL error: {exc}"
    for err in data.get("errors") or []:
        code = ((err.get("extensions") or {}).get("code") or "")
        if code == "IntegrityCheckFailed":
            return False, ("This is a browser session token and Twitch requires an integrity "
                           "check for drops. Use the cookies.jar from the desktop miner.")
        if code in ("Unauthorized", "Forbidden"):
            return False, "Twitch rejected this session"
    current = ((data.get("data") or {}).get("currentUser") or {})
    if not current.get("login"):
        return False, "Twitch did not recognise this account"
    return True, ""


ANDROID_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"


@app.post("/api/twitch/cookies")
async def api_twitch_cookies(request: Request, file: UploadFile = File(...)):
    """Link a Twitch account from the desktop miner's cookies.jar (Android token)."""
    session = _require_auth(request)
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "The file is empty")
    if len(raw) > 1024 * 1024:
        raise HTTPException(400, "That file is too large to be a cookies.jar")

    user_id = session["user_id"]
    user_dir = user_manager.ensure_user_dir(user_id)
    tmp = user_dir / ".cookies_upload.jar"
    try:
        tmp.write_bytes(raw)
    except OSError as exc:
        raise HTTPException(500, f"Could not read the upload: {exc}")
    token = ""
    try:
        import aiohttp
        from yarl import URL
        jar = aiohttp.CookieJar()
        jar.load(tmp)
        for host in ("https://www.twitch.tv", "https://m.twitch.tv"):
            cookies = jar.filter_cookies(URL(host))
            if "auth-token" in cookies:
                token = cookies["auth-token"].value
                break
    except Exception:
        token = ""
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if not token:
        raise HTTPException(400, "That file has no Twitch session (auth-token cookie)")

    info = await _validate_twitch_token(token)
    if not info:
        raise HTTPException(400, "Twitch rejected the session in that file (expired or revoked)")
    client_id = str(info.get("client_id") or "")
    if client_id != ANDROID_CLIENT_ID:
        raise HTTPException(
            400,
            "That session is not from the desktop miner. Run the desktop miner, log in "
            "with the browser and upload the cookies.jar it creates.",
        )
    ok, reason = await _gql_can_mine(token, client_id)
    if not ok:
        raise HTTPException(400, reason)

    # The miner holds the old session in memory; stop it before touching the jar.
    await multi_runner.stop_instance(user_id)
    cookies_path = user_dir / "cookies.jar"
    if cookies_path.exists():
        try:
            (user_dir / "cookies.jar.bak").write_bytes(cookies_path.read_bytes())
        except OSError:
            pass
    try:
        cookies_path.write_bytes(raw)
    except OSError as exc:
        raise HTTPException(500, f"Could not store the session: {exc}")

    settings = _read_settings_data(user_id)
    settings["twitch_client"] = "android"
    try:
        with open(user_dir / "settings.json", "w") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass

    await multi_runner.start_instance(user_id, user_dir)
    logger.info("User %s linked Twitch %s from an uploaded cookies.jar", user_id, info.get("login"))
    return {"success": True, "login": info.get("login"), "scopes": len(info.get("scopes") or [])}



@app.post("/api/twitch/logout")
async def api_twitch_logout(request: Request):
    """Log the user out of Twitch: stop the miner, revoke the token and drop cookies."""
    session = _require_auth(request)
    user_id = session["user_id"]
    user_dir = user_manager.get_user_dir(user_id)
    cookies_path = user_dir / "cookies.jar"

    # Stop the miner first, otherwise it can rewrite the cookie file on shutdown.
    await multi_runner.stop_instance(user_id)

    revoked = False
    if cookies_path.exists():
        revoked = await _revoke_twitch_token(cookies_path)
        try:
            cookies_path.unlink()
        except OSError as exc:
            logger.warning("Could not delete cookies.jar for user %s: %s", user_id, exc)

    # Account specific state, stale once the cookies are gone.
    for name in ("progress.json", "inventory_cache.json"):
        try:
            (user_dir / name).unlink(missing_ok=True)
        except OSError:
            pass

    from web.device_code_manager import DeviceCodeManager
    DeviceCodeManager.get_instance().clear_codes(user_id)

    logger.info("User %s logged out of Twitch (revoked=%s)", user_id, revoked)
    return {"success": True, "revoked": revoked, "linked": False}



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


# ── Branding / DNX Background v1 ──────────────────────────────────────────

BRAND_DIR = DATA_DIR / "branding"
BRAND_DIR.mkdir(exist_ok=True)
BRAND_JSON = BRAND_DIR / "bg.json"

_IMAGE_SIGS = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpg",
    b"RIFF": None,  # webp (check below)
}
_WEBP_MAGIC = b"WEBP"


def _sniff_mime(data: bytes) -> tuple[str, str] | None:
    """Return (mime, ext) or None if not a recognized image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:4] == b"RIFF" and len(data) > 8 and data[8:12] == _WEBP_MAGIC:
        return "image/webp", "webp"
    return None


def _sha8(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()[:8]


def _load_brand_meta() -> dict:
    """Load bg.json; return the stored dict or a default."""
    if BRAND_JSON.exists():
        try:
            return json.loads(BRAND_JSON.read_text())
        except Exception:
            pass
    return {"v": 1, "mode": "none", "choice": "auto", "intensity": 6,
            "tint": "auto", "key": "auto"}


def _save_brand_meta(d: dict) -> None:
    d["saved_at"] = int(datetime.now(timezone.utc).timestamp())
    BRAND_JSON.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


async def _start_runner_and_bg():
    from web.multi_runner import cleanup_orphans
    cleanup_orphans()
    asyncio.create_task(multi_runner.start_monitoring())
    from web.device_code_manager import ensure_mobile_cookie
    all_users = user_manager.list_users()
    for user in all_users:
        user_dir = user_manager.ensure_user_dir(user["id"])
        cookies_path = user_dir / "cookies.jar"
        if cookies_path.exists():
            settings = _read_settings_data(user["id"])
            if settings.get("twitch_client") == "mobile_web":
                ensure_mobile_cookie(user_dir)
            await multi_runner.start_instance(user["id"], user_dir)


@app.post("/api/v1/admin/branding/background")
async def api_branding_upload(request: Request):
    """Upload a processed background image (blob from browser)."""
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    meta_qs = dict(request.query_params)
    raw = await request.body()
    if not raw:
        raise HTTPException(400, "Empty body")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 12 MB)")
    info = _sniff_mime(raw)
    if info is None:
        raise HTTPException(400, "Not a recognized image (PNG/WebP/JPEG)")
    mime, ext = info
    brand_file = BRAND_DIR / f"bg.{ext}"
    brand_file.write_bytes(raw)
    meta = _load_brand_meta()
    meta.update({
        "v": 1, "mode": meta_qs.get("mode", "mask"),
        "choice": meta_qs.get("choice", "auto"),
        "intensity": int(meta_qs.get("intensity", 6)),
        "tint": meta_qs.get("tint", "auto"),
        "key": meta_qs.get("key", "auto"),
        "coverage": float(meta_qs.get("coverage", 0)),
        "params": json.loads(meta_qs.get("params", "{}") or "{}"),
        "sha": _sha8(raw),
        "file": f"bg.{ext}",
        "mime": mime,
    })
    _save_brand_meta(meta)
    return {"success": True, "background": meta}


@app.delete("/api/v1/admin/branding/background")
async def api_branding_delete(request: Request):
    """Remove the background (mode = none)."""
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    for f in BRAND_DIR.glob("bg.*"):
        f.unlink(missing_ok=True)
    meta = _load_brand_meta()
    meta["mode"] = "none"
    _save_brand_meta(meta)
    return {"success": True}


@app.get("/api/v1/branding")
async def api_branding_info():
    """Public branding info (instance_name + background). Used by login & all pages."""
    meta = _load_brand_meta()
    has_file = (BRAND_DIR / meta.get("file", "")).exists() if meta.get("file") else False
    bg = None
    if meta.get("mode") and meta["mode"] != "none" and meta.get("sha") and has_file:
        bg = {
            "mode": meta["mode"],
            "choice": meta.get("choice", "auto"),
            "intensity": meta.get("intensity", 6),
            "tint": meta.get("tint", "auto"),
            "key": meta.get("key", "auto"),
            "sha": meta["sha"],
            "coverage": meta.get("coverage", 0),
            "params": meta.get("params", {}),
        }
    return {
        "instance_name": "Twitch Drops Miner",
        "background": bg,
    }


@app.get("/branding/bg")
async def api_branding_serve(request: Request):
    """Serve the background image. Public, cached immutable."""
    v = request.query_params.get("v", "")
    meta = _load_brand_meta()
    fname = meta.get("file") or ""
    fpath = BRAND_DIR / fname
    if not fpath.exists() or not meta.get("sha") or meta["mode"] == "none":
        raise HTTPException(404, "No background configured")
    if v and meta.get("sha") != v:
        raise HTTPException(404, "Version mismatch")
    from starlette.responses import FileResponse
    return FileResponse(
        str(fpath),
        media_type=meta.get("mime") or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
    from web.multi_runner import cleanup_orphans
    cleanup_orphans()
    asyncio.create_task(multi_runner.start_monitoring())
    from web.device_code_manager import ensure_mobile_cookie
    all_users = user_manager.list_users()
    for user in all_users:
        user_dir = user_manager.ensure_user_dir(user["id"])
        cookies_path = user_dir / "cookies.jar"
        if cookies_path.exists():
            settings = _read_settings_data(user["id"])
            if settings.get("twitch_client") == "mobile_web":
                # Old jars only had the www.twitch.tv cookie; MOBILE_WEB reads m.twitch.tv.
                ensure_mobile_cookie(user_dir)
            await multi_runner.start_instance(user["id"], user_dir)


_history_task: asyncio.Task | None = None
_auto_update_task: asyncio.Task | None = None
_telegram_task: asyncio.Task | None = None
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
    if not (cfg.get("enabled") and get_token()):
        return
    # Telegram occasionally answers 502/timeouts: retry instead of giving up at boot.
    for attempt in range(3):
        bot = TelegramBot(user_manager)
        if await bot.start():
            _telegram_bot = bot
            return
        logger.warning("Telegram bot start attempt %d/3 failed", attempt + 1)
        await asyncio.sleep(4)
    logger.warning("Telegram bot not running; the supervisor will retry in a few minutes")


async def _telegram_supervisor() -> None:
    """Restart the bot if it is enabled but not running (network hiccups, 502s...)."""
    while True:
        await asyncio.sleep(300)
        try:
            from web.telegram_bot import get_config, get_token
            cfg = get_config()
            if cfg.get("enabled") and get_token():
                if _telegram_bot is None or not _telegram_bot.running:
                    logger.info("Telegram supervisor: restarting bot")
                    await _start_telegram_bot()
        except Exception as exc:
            logger.debug("Telegram supervisor error: %s", exc)


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
    global _history_task, _auto_update_task, _telegram_task
    await _start_runner_and_bg()
    _history_task = asyncio.create_task(_history_watcher())
    _auto_update_task = asyncio.create_task(_auto_update_loop())
    await _start_telegram_bot()
    _telegram_task = asyncio.create_task(_telegram_supervisor())


@app.on_event("shutdown")
async def shutdown():
    global _history_task, _auto_update_task, _telegram_task, _telegram_bot
    for task in (_history_task, _auto_update_task, _telegram_task):
        if task is not None:
            task.cancel()
    _history_task = None
    _auto_update_task = None
    _telegram_task = None
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