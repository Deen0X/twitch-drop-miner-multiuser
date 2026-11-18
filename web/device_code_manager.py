from __future__ import annotations

import json
import asyncio
import aiohttp
import yarl
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import logging

logger = logging.getLogger("TwitchDrops.DeviceCode")
DEVICE_CODE_STORE = Path(__file__).parent.parent / "data" / "device_codes.json"

# Twitch ANDROID_APP client used by the device code flow (same as upstream miner).
ANDROID_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"


def _load_codes() -> dict:
    if DEVICE_CODE_STORE.exists():
        with open(DEVICE_CODE_STORE) as f:
            return json.load(f)
    return {}


def _save_codes(codes: dict):
    with open(DEVICE_CODE_STORE, "w") as f:
        json.dump(codes, f, indent=2)


@dataclass
class DeviceCodeInfo:
    user_id: int
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: str
    client_id: str = ANDROID_CLIENT_ID  # ANDROID_APP client_id
    interval: int = 5
    status: str = "pending"
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    error: Optional[str] = None


class DeviceCodeManager:
    _instance: Optional["DeviceCodeManager"] = None

    def __init__(self):
        self._codes: dict[str, DeviceCodeInfo] = {}
        self._lock = threading.Lock()
        self._poll_tasks: dict[int, asyncio.Task] = {}
        DeviceCodeManager._instance = self

    @classmethod
    def get_instance(cls) -> "DeviceCodeManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def store_code(self, user_id: int, info: DeviceCodeInfo) -> str:
        code_key = f"{user_id}_{info.device_code[:8]}"
        with self._lock:
            self._codes[code_key] = info
        return code_key

    def get_code(self, user_id: int) -> Optional[DeviceCodeInfo]:
        with self._lock:
            for code in self._codes.values():
                if code.user_id == user_id and code.status == "pending":
                    return code
        return None

    def get_code_any_status(self, user_id: int) -> Optional[DeviceCodeInfo]:
        with self._lock:
            for code in self._codes.values():
                if code.user_id == user_id:
                    return code
        return None

    def get_code_by_key(self, code_key: str) -> Optional[DeviceCodeInfo]:
        with self._lock:
            return self._codes.get(code_key)

    def update_code(self, code_key: str, **updates):
        with self._lock:
            if code_key in self._codes:
                for k, v in updates.items():
                    setattr(self._codes[code_key], k, v)

    async def start_polling(self, user_id: int):
        from web.auth import UserManager
        um = UserManager()
        user_dir = um.get_user_dir(user_id)
        info = self.get_code(user_id)
        if info is None:
            return

        headers = {"Client-ID": info.client_id, "Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "client_id": info.client_id,
            "device_code": info.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }

        max_attempts = 180
        attempt = 0
        while True:
            attempt += 1
            if attempt > max_attempts:
                info.error = "Code expired (timeout after 15 minutes)"
                info.status = "failed"
                break
            await asyncio.sleep(info.interval)
            logger.info(f"Polling attempt {attempt} for user {user_id}")
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.post("https://id.twitch.tv/oauth2/token", headers=headers, data=payload) as resp:
                        data = await resp.json()
                        logger.info(f"Response status: {resp.status}, data: {data}")
                        if resp.status == 200:
                            info.access_token = data.get("access_token")
                            info.refresh_token = data.get("refresh_token")
                            info.status = "completed"
                            await self._validate_and_save(user_dir, info)
                            break
                        elif resp.status == 400:
                            error = data.get("message", data.get("error", ""))
                            logger.info(f"400 error response: {error}")
                            if error == "authorization_pending":
                                logger.info("Authorization pending, continuing...")
                                continue
                            elif error == "slow_down":
                                logger.info("Slow down received, increasing interval...")
                                info.interval = min(info.interval * 2, 30)
                                continue
                            elif error == "expired_token":
                                info.error = "Code expired"
                                info.status = "failed"
                                break
                            else:
                                info.error = error
                                info.status = "failed"
                                break
                        elif resp.status == 429:
                            info.error = "Rate limited by Twitch. Please try again later."
                            info.status = "failed"
                            break
            except Exception as e:
                if attempt >= 3:
                    info.error = str(e)
                    info.status = "failed"
                    break

    async def _validate_and_save(self, user_dir: Path, info: DeviceCodeInfo):
        import aiohttp

        async with aiohttp.ClientSession() as http:
            async with http.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {info.access_token}"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("client_id") == info.client_id:
                        cookies_file = user_dir / "cookies.jar"
                        cookie_jar = aiohttp.CookieJar()
                        # ANDROID_APP uses https://www.twitch.tv as CLIENT_URL
                        from yarl import URL
                        cookie_jar.update_cookies(
                            {"auth-token": info.access_token, "persistent": str(info.user_id)},
                            URL("https://www.twitch.tv")
                        )
                        cookie_jar.save(cookies_file)
                        await self._restart_miner(info.user_id, user_dir)
                    else:
                        info.error = "Client ID mismatch - invalid token"
                        info.status = "failed"
                else:
                    info.error = "Token validation failed"
                    info.status = "failed"

    async def _restart_miner(self, user_id: int, user_dir: Path) -> None:
        """The miner only reads cookies at startup, so restart it after linking."""
        try:
            from web.auth import UserManager
            from web.multi_runner import MultiRunner
            user = UserManager().get_user(user_id)
            if user and not user.get("is_active", 1):
                return
            await MultiRunner.get_instance().start_instance(user_id, user_dir)
            logger.info(f"Restarted miner for user {user_id} after linking Twitch account")
        except Exception as exc:
            logger.warning(f"Could not restart miner for user {user_id}: {exc}")

    def clear_codes(self, user_id: int) -> int:
        """Forget any pending/finished device code so a logged out user can relink."""
        with self._lock:
            keys = [key for key, info in self._codes.items() if info.user_id == user_id]
            for key in keys:
                del self._codes[key]
        task = self._poll_tasks.pop(user_id, None)
        if task is not None and not task.done():
            task.cancel()
        return len(keys)

    async def generate_code(self, user_id: int) -> DeviceCodeInfo:
        client_id = ANDROID_CLIENT_ID  # ANDROID_APP client_id
        headers = {"Client-ID": client_id, "Content-Type": "application/x-www-form-urlencoded"}
        payload = {"client_id": client_id, "scopes": ""}

        async with aiohttp.ClientSession() as http:
            async with http.post("https://id.twitch.tv/oauth2/device", headers=headers, data=payload) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise Exception(data.get("message", "Failed to generate code"))

                expires_at = datetime.now(timezone.utc).timestamp() + data.get("expires_in", 1800)
                info = DeviceCodeInfo(
                    user_id=user_id,
                    device_code=data["device_code"],
                    user_code=data["user_code"],
                    verification_uri=data["verification_uri"],
                    expires_at=datetime.fromtimestamp(expires_at).isoformat(),
                    interval=data.get("interval", 5)
                )
                self.store_code(user_id, info)
                task = asyncio.create_task(self._polling_wrapper(user_id))
                logger.info(f"Polling task created for user {user_id}")
                return info

    async def _polling_wrapper(self, user_id: int):
        logger.info(f"Polling wrapper started for user {user_id}")
        try:
            await self.start_polling(user_id)
            logger.info(f"Polling wrapper finished for user {user_id}")
        except Exception as e:
            logger.error(f"Polling wrapper error for user {user_id}: {e}")


dm = DeviceCodeManager.get_instance()