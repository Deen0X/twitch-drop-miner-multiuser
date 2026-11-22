"""Telegram bot for Twitch Drop Miner Multiuser.

Global bot, long polling (no webhook), aiohttp + Pillow only.
Only linked users get responses; unknown users are ignored.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageOps

logger = logging.getLogger("TwitchDrops.Telegram")

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "telegram_config.json"
TOKEN_PATH = DATA_DIR / ".telegram_token"
IMAGE_CACHE_DIR = DATA_DIR / "telegram_image_cache"

IMG_W, IMG_H = 600, 800
HTTP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {"enabled": False, "drop_message": "🎉 Drop conseguido con Drop Miner", "bot_username": ""}
API = "https://api.telegram.org/bot{token}/{method}"


def _load_json(path: Path, default: dict) -> dict:
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return dict(default)


def get_config() -> dict:
    cfg = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "drop_message": cfg.get("drop_message") or DEFAULT_CONFIG["drop_message"],
        "bot_username": cfg.get("bot_username", ""),
    }


def set_config(**kwargs) -> dict:
    cfg = get_config()
    for key in ("enabled", "drop_message", "bot_username"):
        if key in kwargs:
            cfg[key] = kwargs[key]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def get_token() -> str:
    try:
        if TOKEN_PATH.exists():
            return TOKEN_PATH.read_text().strip()
    except Exception:
        pass
    return ""


def set_token(token: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token.strip() + "\n")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except Exception:
        pass


def progress_bar(pct: int | float, width: int = 10) -> str:
    value = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(value / 100.0 * width))
    return "▰" * filled + "▱" * (width - filled)


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _campaign_state(campaign: dict) -> tuple[bool, bool]:
    """Return (active, upcoming) based on dates, falling back to flags.

    A campaign can be 'finished' (all drops claimed) and still be active;
    the web shows it when the Active filter is on.
    """
    now = datetime.now(timezone.utc)
    starts = _parse_dt(campaign.get("starts_at"))
    ends = _parse_dt(campaign.get("ends_at"))
    if starts and ends:
        active = starts <= now < ends
        upcoming = now < starts
    else:
        active = bool(campaign.get("active"))
        upcoming = bool(campaign.get("upcoming"))
    if campaign.get("expired"):
        active = False
        upcoming = False
    return active, upcoming


def _wanted_game(campaign: dict, settings: dict) -> bool:
    """Mirror the web 'Only my games' filter: Priority minus Excluded."""
    if settings.get("inventory_only_mine", True) is False:
        return True
    game = str((campaign.get("game") or {}).get("name") or "").strip().lower()
    if not game:
        return True
    exclude = {str(x).strip().lower() for x in (settings.get("exclude") or [])}
    if game in exclude:
        return False
    priority = {str(x).strip().lower() for x in (settings.get("priority") or [])}
    if priority and game not in priority:
        return False
    return True


def _linked_allowed(campaign: dict, settings: dict) -> bool:
    """Respect the web 'Not Linked' filter; by default only linked campaigns."""
    filters = settings.get("inventory_filters") or {}
    if filters.get("not_linked", False):
        return True
    return bool(campaign.get("linked"))


def _user_settings_path(user_manager, user_id: int) -> Path:
    return user_manager.ensure_user_dir(user_id) / "settings.json"


def _read_user_settings(user_manager, user_id: int) -> dict:
    path = _user_settings_path(user_manager, user_id)
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_user_settings(user_manager, user_id: int, data: dict) -> None:
    path = _user_settings_path(user_manager, user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _cache_path(url: str) -> Path:
    key = hashlib.sha1(("cover600x800-v1:" + url).encode("utf-8")).hexdigest()
    return IMAGE_CACHE_DIR / f"{key}.jpg"


def _normalize_bytes(data: bytes) -> Optional[bytes]:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    try:
        canvas = ImageOps.fit(img, (IMG_W, IMG_H), Image.Resampling.LANCZOS)
    except Exception:
        canvas = Image.new("RGB", (IMG_W, IMG_H), (18, 24, 33))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()


async def _fetch_bytes(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": HTTP_UA},
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        return None
    return None


async def normalized_image(url: str) -> Optional[bytes]:
    if not url:
        return None
    path = _cache_path(url)
    try:
        if path.exists():
            return path.read_bytes()
    except Exception:
        pass
    raw = await _fetch_bytes(url)
    if not raw:
        return None
    data = await asyncio.to_thread(_normalize_bytes, raw)
    if data:
        logger.info("Normalized image %s -> %dx%d (%d bytes)", url, IMG_W, IMG_H, len(data))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception:
            pass
    return data


async def compose_drop_image(campaign_url: str, drop_url: str) -> Optional[bytes]:
    campaign_bytes, drop_bytes = await asyncio.gather(
        _fetch_bytes(campaign_url), _fetch_bytes(drop_url)
    )
    if not campaign_bytes:
        return None

    def build() -> bytes:
        W, H = IMG_W, IMG_H
        base = _normalize_bytes(campaign_bytes)
        try:
            canvas = Image.open(io.BytesIO(base)).convert("RGB") if base else None
        except Exception:
            canvas = None
        if canvas is None:
            canvas = Image.new("RGB", (W, H), (18, 24, 33))
        if drop_bytes:
            try:
                drop = Image.open(io.BytesIO(drop_bytes)).convert("RGB")
                size = 180
                thumb = ImageOps.fit(drop, (size, size), Image.Resampling.LANCZOS)
                border = 4
                x = W - size - 24
                y = H - size - 24
                draw = ImageDraw.Draw(canvas)
                draw.rectangle([x - border, y - border, x + size + border - 1, y + size + border - 1],
                               fill=(255, 255, 255))
                canvas.paste(thumb, (x, y))
                draw.ellipse([x + size - 22, y - 14, x + size + 6, y + 14], fill=(34, 197, 94))
                draw.line([x + size - 15, y, x + size - 7, y + 8], fill=(255, 255, 255), width=3)
                draw.line([x + size - 7, y + 8, x + size + 2, y - 6], fill=(255, 255, 255), width=3)
            except Exception:
                pass
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()

    return await asyncio.to_thread(build)


class TelegramBot:
    def __init__(self, user_manager):
        self.user_manager = user_manager
        self.token = get_token()
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._offset = 0

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> bool:
        if self._running:
            return True
        if not self.token:
            return False
        me = await self.get_me()
        if not me:
            logger.warning("Telegram getMe failed (invalid token or Telegram unreachable)")
            return False
        set_config(bot_username=me.get("username", ""))
        self._session = aiohttp.ClientSession()
        await self._prime_offset()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram bot started as @%s", me.get("username", "?"))
        return True

    async def _prime_offset(self) -> None:
        """Discard pending updates so a restart doesn't replay old commands."""
        try:
            result = await self._api(
                "getUpdates",
                data={"offset": "-1", "limit": "1", "timeout": "0"},
            )
            if result:
                last = max(int(update.get("update_id", 0)) for update in result)
                self._offset = last + 1
        except Exception:
            pass

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None

    async def restart(self) -> bool:
        await self.stop()
        self.token = get_token()
        return await self.start()

    async def get_me(self) -> Optional[dict]:
        if not self.token:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(API.format(token=self.token, method="getMe"),
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    data = await resp.json(content_type=None)
                    if not data.get("ok"):
                        logger.warning(
                            "getMe rejected: HTTP %s %s",
                            resp.status,
                            data.get("description") or data,
                        )
                        return None
            return data.get("result")
        except Exception as exc:
            logger.warning("getMe failed: %s", exc)
            return None

    async def _api(self, method: str, *, data=None, params=None) -> Optional[dict]:
        if not self._session or not self.token:
            return None
        try:
            async with self._session.post(API.format(token=self.token, method=method),
                                          data=data, params=params,
                                          timeout=aiohttp.ClientTimeout(total=30)) as resp:
                payload = await resp.json()
            if not payload.get("ok"):
                logger.warning("Telegram %s error: %s", method, payload)
                return None
            return payload.get("result")
        except Exception as exc:
            logger.warning("Telegram %s failed: %s", method, exc)
            return None

    async def send_message(self, chat_id: int, text: str) -> None:
        await self._api("sendMessage", data={
            "chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })

    async def send_photo_url(self, chat_id: int, photo_url: str, caption: str) -> None:
        await self._api("sendPhoto", data={
            "chat_id": str(chat_id), "photo": photo_url, "caption": caption,
            "parse_mode": "HTML",
        })

    async def send_photo_bytes(self, chat_id: int, image: bytes, caption: str,
                               filename: str = "image.jpg") -> None:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption)
        form.add_field("parse_mode", "HTML")
        form.add_field("photo", image, filename=filename, content_type="image/jpeg")
        await self._api("sendPhoto", data=form)

    async def send_campaign_photo(self, chat_id: int, url: str, caption: str) -> None:
        image = await normalized_image(url)
        if image:
            await self.send_photo_bytes(int(chat_id), image, caption, filename="campaign.jpg")
        else:
            await self.send_message(int(chat_id), caption)

    def _find_user(self, from_user: dict) -> Optional[dict]:
        uid = str(from_user.get("id", ""))
        username = (from_user.get("username") or "").lstrip("@").lower()
        users = self.user_manager.list_users(include_inactive=True)
        # Prefer numeric ID matches.
        for user in users:
            settings = _read_user_settings(self.user_manager, user["id"])
            stored_id = str(settings.get("telegram_user_id") or "").strip()
            if stored_id and stored_id == uid:
                return user
        if username:
            for user in users:
                settings = _read_user_settings(self.user_manager, user["id"])
                stored = str(settings.get("telegram_username") or "").lstrip("@").lower()
                if stored and stored == username:
                    return user
        return None

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message")
        if not message:
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        from_user = message.get("from") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return

        user = self._find_user(from_user)
        if user is None:
            return  # silence for unknown users

        # Persist chat_id on first authorized interaction.
        settings = _read_user_settings(self.user_manager, user["id"])
        changed = False
        if str(settings.get("telegram_chat_id") or "") != str(chat_id):
            settings["telegram_chat_id"] = chat_id
            changed = True
        if not settings.get("telegram_user_id") and from_user.get("id"):
            settings["telegram_user_id"] = str(from_user.get("id"))
            changed = True
        if not settings.get("telegram_username") and from_user.get("username"):
            settings["telegram_username"] = from_user.get("username")
            changed = True
        if changed:
            _write_user_settings(self.user_manager, user["id"], settings)

        command = text.split()[0].lower()
        if command == "/start":
            await self.send_message(chat_id,
                f"Linked as <b>{html.escape(user.get('display_name') or user['username'])}</b>.\n"
                "Use /stats to see your campaign progress.")
        elif command == "/help":
            await self.send_message(chat_id, "<b>Commands</b>\n/stats - campaign progress\n/help - this message")
        elif command in ("/stats", "/status"):
            await self._send_status(user["id"], chat_id)

    def _find_campaign(self, campaigns: list, current: dict) -> Optional[dict]:
        cid = str(current.get("id") or "")
        cname = current.get("name") or ""
        cgame = current.get("game") or ""
        for campaign in campaigns:
            if cid and str(campaign.get("id")) == cid:
                return campaign
        for campaign in campaigns:
            if (campaign.get("name") or "") == cname and ((campaign.get("game") or {}).get("name") or "") == cgame:
                return campaign
        for campaign in campaigns:
            if (campaign.get("name") or "") == cname:
                return campaign
        return None

    def _campaign_caption(self, campaign: Optional[dict], current: dict, progress_drops: dict) -> str:
        name = (campaign or {}).get("name") or current.get("name") or "Campaign"
        game = ((campaign or {}).get("game") or {}).get("name") or current.get("game")
        lines = [f"<b>{html.escape(name)}</b>"]
        if game:
            lines.append(html.escape(game))
        drops = (campaign or {}).get("drops") or []
        if drops:
            for drop in drops:
                dp = progress_drops.get(str(drop.get("id")), {}) if isinstance(progress_drops, dict) else {}
                required = dp.get("required_minutes", drop.get("required_minutes") or 0) or 0
                current_minutes = dp.get("current_minutes", drop.get("current_minutes") or 0) or 0
                pct = int(round((current_minutes / required * 100) if required else 0))
                pct = max(0, min(100, pct))
                claimed = bool(dp.get("is_claimed", drop.get("is_claimed")))
                mark = " ✓" if claimed else ""
                lines.append(
                    f"<code>[{progress_bar(pct)}] {pct:03d}%  "
                    f"{html.escape(drop.get('name') or '')}{mark}</code>"
                )
        else:
            for drop in (progress_drops or {}).values():
                required = drop.get("required_minutes") or 0
                current_minutes = drop.get("current_minutes") or 0
                pct = int(round((current_minutes / required * 100) if required else 0))
                pct = max(0, min(100, pct))
                mark = " ✓" if drop.get("is_claimed") else ""
                lines.append(
                    f"<code>[{progress_bar(pct)}] {pct:03d}%  "
                    f"{html.escape(drop.get('name') or '')}{mark}</code>"
                )
        caption = "\n".join(lines)
        return caption[:997] + "..." if len(caption) > 1000 else caption

    def _miner_notice(self, user_id: int, cache_file: Path, campaigns: list) -> Optional[str]:
        """Explain that the miner is still booting instead of reporting 'no campaigns'."""
        running, state, started_at = True, "", 0.0
        try:
            from web.multi_runner import MultiRunner
            snap = MultiRunner.get_instance().get_instance_snapshot(user_id)
            running = bool(snap.get("running", True))
            state = str(snap.get("state") or "")
            started_at = float(snap.get("started_at") or 0)
        except Exception:
            running, state, started_at = True, "", 0.0

        try:
            mtime = cache_file.stat().st_mtime if cache_file.exists() else 0.0
        except Exception:
            mtime = 0.0

        # The cache belongs to this miner run only if it was written after boot.
        if not running or state == "stopped":
            return "The miner is not running. Start it from the web dashboard and send /stats again."
        if state.startswith("error"):
            detail = state.split(":", 1)[-1].strip() or state
            return f"The miner failed to start ({detail}). Check the web dashboard."
        if campaigns and mtime > started_at:
            return None
        return "The miner is starting: campaigns are not loaded yet. Send /stats again in a minute."

    async def _send_status(self, user_id: int, chat_id: int) -> None:
        user_dir = self.user_manager.ensure_user_dir(user_id)
        cache_file = user_dir / "inventory_cache.json"
        campaigns = []
        if cache_file.exists():
            try:
                with open(cache_file) as f:
                    campaigns = json.load(f)
            except Exception:
                campaigns = []
        if not isinstance(campaigns, list):
            campaigns = []

        notice = self._miner_notice(user_id, cache_file, campaigns)
        if notice:
            await self.send_message(chat_id, notice)
            return

        progress = {}
        progress_file = user_dir / "progress.json"
        if progress_file.exists():
            try:
                with open(progress_file) as f:
                    progress = json.load(f)
            except Exception:
                progress = {}

        settings = _read_user_settings(self.user_manager, user_id)
        current_on = bool(settings.get("telegram_notify_current", True))
        upcoming_on = bool(settings.get("telegram_notify_upcoming", False))
        if not current_on and not upcoming_on:
            await self.send_message(
                chat_id,
                "No campaign types enabled. Turn on Current and/or Upcoming in the web Telegram section."
            )
            return

        progress_drops = progress.get("drops") or {}
        current_campaign = progress.get("current_campaign") if isinstance(progress, dict) else None
        shown = 0

        for campaign in campaigns:
            if shown >= 10:
                break
            if not isinstance(campaign, dict):
                continue
            is_active, is_upcoming = _campaign_state(campaign)
            if not (is_active or is_upcoming):
                continue
            if not _wanted_game(campaign, settings):
                continue
            if not _linked_allowed(campaign, settings):
                continue
            if is_active and not current_on:
                continue
            if is_upcoming and not upcoming_on:
                continue

            if is_upcoming:
                name = campaign.get("name") or "Campaign"
                game = (campaign.get("game") or {}).get("name") or ""
                lines = ["<b>Upcoming campaign</b>", html.escape(name)]
                if game:
                    lines.append(html.escape(game))
                starts = campaign.get("starts_at")
                if starts:
                    lines.append(f"Starts: {html.escape(str(starts)[:16].replace('T', ' '))}")
                caption = "\n".join(lines)
            else:
                same_current = bool(
                    current_campaign and (
                        str(campaign.get("id")) == str(current_campaign.get("id"))
                        or (campaign.get("name") == current_campaign.get("name"))
                    )
                )
                caption = self._campaign_caption(
                    campaign,
                    current_campaign if same_current else {},
                    progress_drops if same_current else {},
                )

            image = campaign.get("image_url") or ""
            if image:
                await self.send_campaign_photo(chat_id, image, caption)
            else:
                await self.send_message(chat_id, caption)
            shown += 1
            await asyncio.sleep(0.25)

        if shown == 0:
            await self.send_message(chat_id, "No active or upcoming campaigns for your games.")

    async def notify_drop(self, user_id: int, entry: dict) -> None:
        cfg = get_config()
        if not cfg.get("enabled") or not self.running:
            return
        settings = _read_user_settings(self.user_manager, user_id)
        chat_id = settings.get("telegram_chat_id")
        if not chat_id or settings.get("telegram_notifications") is False:
            return
        if settings.get("telegram_notify_current", True) is False:
            return
        campaign = entry.get("campaign") or ""
        drop = entry.get("drop") or ""
        message = cfg.get("drop_message") or DEFAULT_CONFIG["drop_message"]
        caption = f"<b>{html.escape(campaign)}</b>\n{html.escape(drop)}\n{html.escape(message)}"
        image = await compose_drop_image(entry.get("campaign_image") or "", entry.get("image") or "")
        if image:
            await self.send_photo_bytes(int(chat_id), image, caption, filename="drop.jpg")
        else:
            await self.send_message(int(chat_id), caption)

    async def notify_upcoming(self, user_id: int) -> None:
        cfg = get_config()
        if not cfg.get("enabled") or not self.running:
            return
        settings = _read_user_settings(self.user_manager, user_id)
        if settings.get("telegram_notifications") is False:
            return
        chat_id = settings.get("telegram_chat_id")
        user_dir = self.user_manager.ensure_user_dir(user_id)
        cache_file = user_dir / "inventory_cache.json"
        if not cache_file.exists():
            return
        try:
            with open(cache_file) as f:
                campaigns = json.load(f)
        except Exception:
            return
        if not isinstance(campaigns, list):
            return

        upcoming = []
        for c in campaigns:
            if not isinstance(c, dict):
                continue
            _, is_upcoming = _campaign_state(c)
            if is_upcoming and _wanted_game(c, settings) and _linked_allowed(c, settings):
                upcoming.append(c)
        first_run = "telegram_seen_campaigns" not in settings
        seen = {str(x) for x in (settings.get("telegram_seen_campaigns") or [])}
        current_ids = [str(c.get("id")) for c in upcoming]
        new = [c for c in upcoming if str(c.get("id")) not in seen]

        if first_run or set(current_ids) != seen:
            settings["telegram_seen_campaigns"] = current_ids[-500:]
            _write_user_settings(self.user_manager, user_id, settings)

        if first_run or not chat_id:
            return
        if settings.get("telegram_notify_upcoming") is not True:
            return

        for campaign in new:
            caption = f"<b>Upcoming campaign</b>\n{html.escape(campaign.get('name') or '')}"
            game = (campaign.get("game") or {}).get("name")
            if game:
                caption += f"\n{html.escape(game)}"
            starts = campaign.get("starts_at")
            if starts:
                caption += f"\nStarts: {html.escape(str(starts)[:16].replace('T', ' '))}"
            image = campaign.get("image_url") or ""
            if image:
                await self.send_campaign_photo(int(chat_id), image, caption)
            else:
                await self.send_message(int(chat_id), caption)
            await asyncio.sleep(0.25)

    async def notify_new_campaign(self, user_id: int) -> None:
        """Notify the user when a new active campaign appears for their games."""
        cfg = get_config()
        if not cfg.get("enabled") or not self.running:
            return
        settings = _read_user_settings(self.user_manager, user_id)
        if settings.get("telegram_notifications") is False:
            return
        if settings.get("telegram_notify_current", True) is not True:
            return
        chat_id = settings.get("telegram_chat_id")
        user_dir = self.user_manager.ensure_user_dir(user_id)
        cache_file = user_dir / "inventory_cache.json"
        if not cache_file.exists():
            return
        try:
            with open(cache_file) as f:
                campaigns = json.load(f)
        except Exception:
            return
        if not isinstance(campaigns, list):
            return

        # Mirror the web inventory filters: active + wanted game + linked allowed.
        active = []
        for c in campaigns:
            if not isinstance(c, dict):
                continue
            is_active, _ = _campaign_state(c)
            if not is_active:
                continue
            if not _wanted_game(c, settings):
                continue
            if not _linked_allowed(c, settings):
                continue
            active.append(c)

        first_run = "telegram_seen_active_campaigns" not in settings
        seen = {str(x) for x in (settings.get("telegram_seen_active_campaigns") or [])}
        current_ids = [str(c.get("id")) for c in active]
        new = [c for c in active if str(c.get("id")) not in seen]

        if first_run or set(current_ids) != seen:
            settings["telegram_seen_active_campaigns"] = current_ids[-500:]
            _write_user_settings(self.user_manager, user_id, settings)

        if first_run or not chat_id:
            return

        progress = {}
        progress_file = user_dir / "progress.json"
        if progress_file.exists():
            try:
                with open(progress_file) as f:
                    progress = json.load(f)
            except Exception:
                progress = {}
        progress_drops = progress.get("drops") or {} if isinstance(progress, dict) else {}
        current_campaign = progress.get("current_campaign") if isinstance(progress, dict) else None

        for campaign in new:
            caption = "Nueva campaña!\n" + self._campaign_caption(
                campaign,
                current_campaign if (current_campaign and (
                    str(campaign.get("id")) == str(current_campaign.get("id"))
                    or campaign.get("name") == current_campaign.get("name")
                )) else {},
                progress_drops if (current_campaign and (
                    str(campaign.get("id")) == str(current_campaign.get("id"))
                    or campaign.get("name") == current_campaign.get("name")
                )) else {},
            )
            image = campaign.get("image_url") or ""
            if image:
                await self.send_campaign_photo(int(chat_id), image, caption)
            else:
                await self.send_message(int(chat_id), caption)
            await asyncio.sleep(0.25)

    async def _poll_loop(self) -> None:
        assert self._session is not None
        while self._running:
            try:
                result = await self._api(
                    "getUpdates",
                    data={"offset": str(self._offset), "timeout": "25", "allowed_updates": '["message"]'},
                )
                if result is None:
                    await asyncio.sleep(3)
                    continue
                for update in result:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    try:
                        await self._handle_update(update)
                    except Exception as exc:
                        logger.warning("Telegram update error: %s", exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Telegram poll error: %s", exc)
                await asyncio.sleep(5)
