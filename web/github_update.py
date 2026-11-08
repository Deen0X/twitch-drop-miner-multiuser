from __future__ import annotations

import asyncio
import logging
import json
import shutil
import zipfile
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("TwitchDrops.GitHubUpdate")

GITHUB_REPO = "DevilXD/TwitchDropsMiner"
DEV_BRANCH_URL = "https://github.com/DevilXD/TwitchDropsMiner/releases/tag/dev-build"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"

DATA_DIR = Path(__file__).parent.parent / "data"
UPDATED_SOURCE_DIR = DATA_DIR / "updated_source"
BACKUP_DIR = DATA_DIR / "backup"


@dataclass
class UpdateResult:
    success: bool
    version: str = ""
    message: str = ""
    backup_path: Optional[Path] = None


@dataclass
class Patch:
    file: str
    replacements: dict[str, str] = field(default_factory=dict)
    inserts: dict[str, list[str]] = field(default_factory=dict)


class GitHubUpdater:
    def __init__(self, github_token: Optional[str] = None):
        self.github_token = github_token
        self.updated_source = UPDATED_SOURCE_DIR
        self.backup_dir = BACKUP_DIR
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _load_patches(self) -> dict[str, Patch]:
        patches_file = Path(__file__).parent.parent / "config" / "patches.json"
        if not patches_file.exists():
            return {}
        with open(patches_file) as f:
            data = json.load(f)
        result = {}
        for file, patch_data in data.items():
            result[file] = Patch(
                file=file,
                replacements=patch_data.get("replace", {}),
                inserts=patch_data.get("insert_after", {})
            )
        return result

    def _apply_patches(self, source_dir: Path) -> list[str]:
        patches = self._load_patches()
        applied = []
        for file, patch in patches.items():
            file_path = source_dir / file
            if not file_path.exists():
                logger.warning(f"Patch target not found: {file}")
                continue
            with open(file_path, encoding="utf-8") as f:
                lines = f.readlines()
            new_lines = []
            i = 0
            while i < len(lines):
                line = lines[i]
                replaced = False
                for old, new in patch.replacements.items():
                    if old in line:
                        new_lines.append(line.replace(old, new))
                        applied.append(f"{file}: replaced line")
                        replaced = True
                        break
                if not replaced:
                    new_lines.append(line)
                i += 1
            with open(file_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        return applied

    def _backup_current(self) -> Optional[Path]:
        main_py = Path(__file__).parent.parent / "main.py"
        if not main_py.exists():
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = self.backup_dir / f"source_{timestamp}"
        backup_path.mkdir(parents=True, exist_ok=True)
        project_root = Path(__file__).parent.parent
        for item in ["main.py", "gui.py", "twitch.py", "websocket.py", "settings.py",
                     "constants.py", "inventory.py", "channel.py", "cache.py",
                     "registry.py", "utils.py", "exceptions.py", "translate.py",
                     "version.py", "requirements.txt", "README.md", "constants.py",
                     "Dockerfile", "docker-compose.yml", "build.spec", "build.bat",
                     "build.sh", "setup_env.sh", "setup_env.bat"]:
            src = project_root / item
            if src.exists():
                dst = backup_path / item
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        logger.info(f"Backup created at {backup_path}")
        return backup_path

    async def check_update(self, dev_build: bool = True) -> dict:
        try:
            from ghapi.all import GhApi
        except ImportError:
            try:
                from github import Github
            except ImportError:
                return {"available": False, "error": "pygithub not installed"}

        headers = {"Authorization": f"token {self.github_token}"} if self.github_token else {}

        if dev_build:
            url = DEV_BRANCH_URL
        else:
            url = RELEASES_URL

        return {
            "available": True,
            "url": url,
            "dev_build": dev_build,
            "message": f"Check {url} for latest version"
        }

    async def download_and_apply(self, dev_build: bool = True) -> UpdateResult:
        backup = self._backup_current()
        if backup is None:
            return UpdateResult(success=False, message="main.py not found - cannot proceed")

        self.updated_source.mkdir(parents=True, exist_ok=True)

        try:
            from ghapi.all import GhApi
            api = GhApi(token=self.github_token) if self.github_token else GhApi()
        except ImportError:
            try:
                from github import Github
                g = Github(self.github_token) if self.github_token else Github()
                repo = g.get_repo(GITHUB_REPO)
            except ImportError:
                return UpdateResult(success=False, message="pygithub not installed", backup_path=backup)

        try:
            if dev_build:
                release = api.repos.get_latest_release(owner="DevilXD", repo="TwitchDropsMiner")
            else:
                releases = api.repos.list_releases(owner="DevilXD", repo="TwitchDropsMiner")
                release = releases[0]
        except Exception as e:
            return UpdateResult(success=False, message=f"Failed to fetch release: {e}", backup_path=backup)

        version = release.tag_name or "unknown"
        assets = release.assets or []

        zip_asset = None
        for asset in assets:
            if asset.name.endswith(".zip"):
                zip_asset = asset
                break

        if zip_asset is None:
            return UpdateResult(success=False, message="No zip asset found in release", backup_path=backup)

        try:
            import requests
            zip_url = zip_asset.browser_download_url
            zip_path = self.updated_source / "update.zip"
            response = requests.get(zip_url, headers=headers, timeout=60)
            response.raise_for_status()
            with open(zip_path, "wb") as f:
                f.write(response.content)
        except Exception as e:
            return UpdateResult(success=False, message=f"Failed to download zip: {e}", backup_path=backup)

        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                z.extractall(self.updated_source)
            zip_path.unlink()
        except Exception as e:
            return UpdateResult(success=False, message=f"Failed to extract zip: {e}", backup_path=backup)

        extracted = self.updated_source
        for item in extracted.iterdir():
            if item.is_dir() and (item / "main.py").exists():
                extracted = item
                break

        applied = self._apply_patches(extracted)

        project_root = Path(__file__).parent.parent
        for item in extracted.iterdir():
            dst = project_root / item.name
            if dst.is_dir():
                shutil.rmtree(dst, ignore_errors=True)
            elif dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)

        shutil.rmtree(self.updated_source, ignore_errors=True)

        return UpdateResult(
            success=True,
            version=version,
            message=f"Updated to {version}. {len(applied)} patches applied.",
            backup_path=backup
        )