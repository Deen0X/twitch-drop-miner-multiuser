#!/usr/bin/env python
import sys
import asyncio
import logging
import os
import json
import time
from pathlib import Path

# Change to the project root directory so relative paths work correctly
PROJECT_ROOT = Path(__file__).parent.parent
os.chdir(PROJECT_ROOT)

# Add to path FIRST before importing anything
sys.path.insert(0, str(PROJECT_ROOT))

user_id = int(sys.argv[1])
user_dir = Path(sys.argv[2])
cookies_path = user_dir / "cookies.jar"
progress_file = user_dir / "progress.json"

# Override WORKING_DIR, LANG_PATH and COOKIES_PATH in constants BEFORE importing twitch
# WORKING_DIR must be PROJECT_ROOT so resource_path() finds icons/assets correctly
import constants as constants_module
constants_module.WORKING_DIR = PROJECT_ROOT
constants_module.LANG_PATH = PROJECT_ROOT / "lang"
constants_module.COOKIES_PATH = cookies_path
# One cache per user: the shared web/cache/mapping.json was written by every
# miner at once and a lost race killed the process (FileNotFoundError on replace).
constants_module.CACHE_PATH = user_dir / "cache"
constants_module.CACHE_DB = constants_module.CACHE_PATH / "mapping.json"

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s: %(message)s')

# Progress tracking
_last_progress_write = 0
_progress_data = {}
_last_inventory_write = 0
_inventory_cache = []

def serialize_campaign(campaign):
    return {
        "id": campaign.id,
        "name": campaign.name,
        "image_url": campaign.image_url,
        "starts_at": campaign.starts_at.isoformat() if hasattr(campaign.starts_at, 'isoformat') else str(campaign.starts_at),
        "ends_at": campaign.ends_at.isoformat() if hasattr(campaign.ends_at, 'isoformat') else str(campaign.ends_at),
        "active": campaign.active,
        "upcoming": campaign.upcoming,
        "expired": campaign.expired,
        "linked": campaign.linked,
        "eligible": campaign.eligible,
        "finished": campaign.finished,
        "link_url": getattr(campaign, 'link_url', ''),
        "game": {
            "id": campaign.game.id,
            "name": campaign.game.name,
            "excluded": getattr(campaign.game, 'excluded', False)
        },
        "drops": [{
            "id": drop.id,
            "name": drop.name,
            "image_url": campaign.image_url,
            "benefit_images": [b.image_url for b in drop.benefits] if hasattr(drop, 'benefits') else [],
            "current_minutes": drop.current_minutes,
            "required_minutes": drop.required_minutes,
            "progress": drop.progress,
            "can_claim": drop.can_claim,
            "is_claimed": drop.is_claimed,
            "starts_at": drop.starts_at.isoformat() if hasattr(drop.starts_at, 'isoformat') else str(drop.starts_at),
            "ends_at": drop.ends_at.isoformat() if hasattr(drop.ends_at, 'isoformat') else str(drop.ends_at),
        } for drop in campaign.drops]
    }

def write_inventory_cache():
    global _last_inventory_write
    now = time.time()
    if now - _last_inventory_write < 30:
        return
    _last_inventory_write = now
    cache_file = user_dir / "inventory_cache.json"
    with open(cache_file, 'w') as f:
        json.dump(_inventory_cache, f, indent=2)

async def main():
    from settings import Settings
    from settings import Settings as SettingsBase
    from twitch import Twitch
    from dataclasses import dataclass

    # Import with full path to avoid relative import issues
    import importlib.util
    inv_spec = importlib.util.spec_from_file_location("inventory", PROJECT_ROOT / "inventory.py")
    inv_module = importlib.util.module_from_spec(inv_spec)
    inv_spec.loader.exec_module(inv_module)
    TimedDrop = inv_module.TimedDrop

    # Delete progress file on startup
    if progress_file.exists():
        progress_file.unlink()

    # Progress tracking
    current_campaign = None

    def on_progress_update(drop):
        global _progress_data, current_campaign
        _progress_data[drop.id] = {
            "name": drop.name,
            "current_minutes": drop.current_minutes,
            "progress": drop.progress,
            "can_claim": drop.can_claim,
            "is_claimed": drop.is_claimed,
            "required_minutes": drop.required_minutes
        }
        if drop.campaign and (not current_campaign or current_campaign.id != drop.campaign.id):
            current_campaign = drop.campaign
        write_progress_file()

    def write_progress_file():
        global _last_progress_write
        now = time.time()
        if now - _last_progress_write < 20:
            return
        _last_progress_write = now
        progress_payload = {
            "last_update": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "drops": _progress_data
        }
        if current_campaign:
            progress_payload["current_campaign"] = {
                "id": current_campaign.id,
                "name": current_campaign.name,
                "game": current_campaign.game.name if current_campaign.game else "",
                "progress": current_campaign.progress if hasattr(current_campaign, 'progress') else 0,
                "current_minutes": current_campaign.required_minutes if hasattr(current_campaign, 'required_minutes') else 0,
                "required_minutes": current_campaign.required_minutes if hasattr(current_campaign, 'required_minutes') else 0
            }
        with open(progress_file, 'w') as f:
            json.dump(progress_payload, f, indent=2)
    
    # Monkey-patch TimedDrop to call our callback
    _original_update_minutes = TimedDrop.update_minutes
    def patched_update_minutes(self, new_minutes):
        result = _original_update_minutes(self, new_minutes)
        on_progress_update(self)
        return result
    TimedDrop.update_minutes = patched_update_minutes
    
    @dataclass
    class Args:
        log = True
        tray = False
        dump = False
        @property
        def logging_level(self): return logging.INFO
        @property
        def debug_ws(self): return logging.DEBUG
        @property
        def debug_gql(self): return logging.DEBUG

    args = Args()
    
    # Load settings from user's settings.json
    settings_path = user_dir / "settings.json"
    settings_data = {}
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings_data = json.load(f)
        except:
            pass
    
    class UserSettings:
        def __init__(self):
            self._data = settings_data.copy()
            self._data.setdefault('log', True)
            self._data.setdefault('tray', False)
            self._data.setdefault('dump', False)
            self._data.setdefault('proxy', None)
            self._data.setdefault('language', 'English')
            self._data.setdefault('dark_mode', False)
            self._data.setdefault('exclude', [])
            self._data.setdefault('priority', [])
            self._data.setdefault('autostart_tray', False)
            self._data.setdefault('connection_quality', 1)
            self._data.setdefault('tray_notifications', True)
            self._data.setdefault('enable_badges_emotes', False)
            self._data.setdefault('available_drops_check', False)
            self._data.setdefault('priority_mode', 0)
            self._data.setdefault('logging_level', 20)
            self._data.setdefault('debug_ws', 10)
            self._data.setdefault('debug_gql', 10)
        def __getattr__(self, name):
            if name in self._data:
                return self._data[name]
            return None
        def save(self, force=False):
            with open(settings_path, 'w') as f:
                json.dump(self._data, f, indent=2)

        @staticmethod
        async def inventory_cache_task(client_ref):
            while True:
                await asyncio.sleep(30)
                try:
                    global _inventory_cache
                    client = client_ref()
                    if client and hasattr(client, 'inventory') and client.inventory:
                        _inventory_cache = [serialize_campaign(c) for c in client.inventory]
                        write_inventory_cache()
                except Exception as e:
                    logging.error(f"Error updating inventory cache: {e}")

    settings = UserSettings()
    settings.save()

    client = Twitch(settings)

    # Surface what the miner is doing: the GUI status is invisible in headless mode.
    state_file = user_dir / "state.json"
    _orig_status_update = client.gui.status.update

    def _status_update(text, *args, **kwargs):
        try:
            with open(state_file, "w") as f:
                json.dump({"status": str(text), "updated": time.time()}, f)
        except Exception:
            pass
        return _orig_status_update(text, *args, **kwargs)

    client.gui.status.update = _status_update

    # The web layer owns account linking (device code). There is no browser in the
    # container, so an expired session must fail with a clear message instead of
    # trying the upstream browser login (zendriver isn't installed here).
    import twitch as twitch_module

    async def _no_browser_login(_self):
        raise RuntimeError(
            "Twitch session missing or invalid. Relink the account from the web Profile page."
        )

    twitch_module._AuthState._browser_login = _no_browser_login

    # The miner validates that the token's client matches its own. Accounts
    # linked with the browser cookie use the web client; older ones the Android
    # app; the (now unusable) device flow used the mobile web client.
    _clients = {
        "web": constants_module.ClientType.WEB,
        "android": constants_module.ClientType.ANDROID_APP,
        "mobile_web": constants_module.ClientType.MOBILE_WEB,
    }
    _client = _clients.get(getattr(settings, "twitch_client", None))
    if _client is not None:
        client._client_type = _client

    # Start inventory cache task with weak reference to client
    cache_task = asyncio.create_task(UserSettings.inventory_cache_task(lambda: client))

    try:
        await client.run()
    except RuntimeError as exc:
        logging.error(f"Miner cannot start: {exc}")
        raise SystemExit(3)

if __name__ == "__main__":
    asyncio.run(main())
