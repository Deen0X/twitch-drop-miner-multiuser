from __future__ import annotations

import sys
import asyncio
import logging
import json
import secrets
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
    ctx = {"request": request, "error": None}
    return Response(tmpl.render(ctx), media_type="text/html")


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    form_data = await request.form()
    remember = form_data.get("remember", "off") == "on"
    user = user_manager.verify_user(username, password)
    if user is None:
        tmpl = templates.env.get_template("login.html")
        ctx = {"request": request, "error": "Invalid username or password"}
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
    tmpl = templates.env.get_template("dashboard.html")
    ctx = {
        "request": request,
        "user": user,
        "snapshot": snapshot,
        "settings": user_settings,
        "is_admin": session.get("is_admin", False)
    }
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/inventory")
async def inventory_page(request: Request):
    session = _require_auth(request)
    user = user_manager.get_user(session["user_id"])
    if user is None:
        raise HTTPException(404, "User not found")
    tmpl = templates.env.get_template("inventory.html")
    ctx = {"request": request, "user": user, "is_admin": session.get("is_admin", False)}
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/api/status")
async def api_status(request: Request):
    session = _require_auth(request)
    snapshot = multi_runner.get_instance_snapshot(session["user_id"])
    return snapshot


@app.get("/api/inventory")
async def api_inventory(request: Request):
    session = _require_auth(request)
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
    with open(settings_path, "w") as f:
        json.dump(current, f, indent=2)

    cache_file = user_dir / "inventory_cache.json"
    if cache_file.exists():
        cache_file.unlink()

    return {"success": True}


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
        "inventory_filters": inv_filters
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
    ctx = {"request": request, "user": user, "is_admin": session.get("is_admin", False)}
    return Response(tmpl.render(ctx), media_type="text/html")


@app.get("/admin")
async def admin_page(request: Request):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    user = user_manager.get_user(session["user_id"])
    users = user_manager.list_users(include_inactive=True)
    all_status = multi_runner.get_all_snapshots()
    tmpl = templates.env.get_template("admin.html")
    ctx = {
        "request": request,
        "user": user,
        "users": users,
        "status": all_status
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


@app.get("/api/admin/check-update")
async def api_check_update(request: Request, dev_build: bool = True):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    from web.github_update import GitHubUpdater
    updater = GitHubUpdater()
    result = await updater.check_update(dev_build=dev_build)
    return result


@app.post("/api/admin/apply-update")
async def api_apply_update(request: Request, dev_build: bool = Form(True)):
    session = _require_auth(request)
    if not session["is_admin"]:
        raise HTTPException(403, "Admin access required")
    from web.github_update import GitHubUpdater
    updater = GitHubUpdater()
    result = await updater.download_and_apply(dev_build=dev_build)
    return {
        "success": result.success,
        "version": result.version,
        "message": result.message,
        "backup_path": str(result.backup_path) if result.backup_path else None
    }


async def start_runner():
    asyncio.create_task(multi_runner.start_monitoring())
    all_users = user_manager.list_users()
    for user in all_users:
        user_dir = user_manager.ensure_user_dir(user["id"])
        cookies_path = user_dir / "cookies.jar"
        if cookies_path.exists():
            await multi_runner.start_instance(user["id"], user_dir)


@app.on_event("startup")
async def startup():
    await start_runner()


@app.on_event("shutdown")
async def shutdown():
    await multi_runner.shutdown()


def run(port: int = 4295, host: str = "0.0.0.0"):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Twitch Drops Miner Web Server")
    parser.add_argument("--port", type=int, default=4295)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    run(port=args.port, host=args.host)