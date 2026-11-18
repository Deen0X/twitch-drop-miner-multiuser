from __future__ import annotations

import asyncio
import logging
import json
import signal
import sys
import shutil
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger("TwitchDrops.MultiRunner")

DATA_DIR = Path(__file__).parent.parent / "data"


@dataclass
class InstanceStatus:
    user_id: int
    username: str
    running: bool
    state: str
    campaigns_count: int = 0
    active_drop_minutes: int = 0
    current_game: str = ""
    last_update: str = ""


class HeadlessTwitchInstance:
    def __init__(self, user_id: int, user_dir: Path, settings_override: dict | None = None):
        self.user_id = user_id
        self.user_dir = user_dir
        self.settings_override = settings_override or {}
        self._process: Optional[asyncio.subprocess.Process] = None
        self._shutdown_event = asyncio.Event()
        self._started_at: Optional[float] = None
        self._status = InstanceStatus(
            user_id=user_id,
            username="",
            running=False,
            state="stopped"
        )

    @property
    def status(self) -> InstanceStatus:
        return self._status

    async def start(self):
        if self._process is not None and self._process.returncode is None:
            return  # Already running

        self._shutdown_event.clear()
        self._status.running = True
        self._status.state = "starting"
        self._started_at = time.time()

        # Launch as subprocess - Python executable running headless_miner.py
        # We pass user_id and user_dir as arguments
        import sys
        python_exe = sys.executable
        headless_script = Path(__file__).parent / "headless_miner.py"
        
        if not headless_script.exists():
            # Create the headless miner script
            headless_code = f'''#!/usr/bin/env python
import sys
import asyncio
import logging
sys.path.insert(0, str(Path(__file__).parent.parent))

user_id = int(sys.argv[1])
user_dir = Path(sys.argv[2])
cookies_path = user_dir / "cookies.jar"

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s: %(message)s')

async def main():
    from settings import Settings
    from twitch import Twitch
    from dataclasses import dataclass
    
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
    import json
    settings_path = user_dir / "settings.json"
    settings_data = {{}}
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
        @property
        def settings(self):
            return self._data

    settings = UserSettings()
    
    client = Twitch(settings, cookies_path=cookies_path)
    await client.run()

if __name__ == "__main__":
    asyncio.run(main())
'''
            with open(headless_script, "w") as f:
                f.write(headless_code)

        try:
            self._process = await asyncio.create_subprocess_exec(
                python_exe,
                str(headless_script),
                str(self.user_id),
                str(self.user_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE
            )
            self._status.username = ""
            self._status.state = "running"
            logger.info(f"Instance {self.user_id} started as subprocess")
            
            # Read output asynchronously
            asyncio.create_task(self._read_output())
            
        except Exception as e:
            logger.error(f"Instance {self.user_id} failed to start: {e}")
            self._status.state = f"error: {e}"
            self._status.running = False

    async def _read_output(self):
        if self._process is None or self._process.stdout is None:
            return
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                logger.info(line.decode().strip())
        except Exception:
            pass

    async def stop(self):
        logger.info(f"Instance {self.user_id} stop() called")
        self._shutdown_event.set()
        
        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._process.kill()
            except Exception as e:
                logger.info(f"Instance {self.user_id} kill error: {e}")
            self._process = None
        
        self._status.running = False
        self._status.state = "stopped"
        logger.info(f"Instance {self.user_id} stop() complete")

    def get_state_snapshot(self) -> dict:
        if self._process is None:
            return {"user_id": self.user_id, "running": False, "state": "stopped", "started_at": 0}
        return {
            "user_id": self.user_id,
            "running": self._process.returncode is None,
            "state": self._status.state,
            "started_at": self._started_at or 0,
            "campaigns_count": 0,
            "active_drop_minutes": 0,
            "current_game": "",
            "inventory": {},
            "last_update": datetime.now(timezone.utc).isoformat()
        }

    def _get_inventory_snapshot(self) -> dict:
        return {}

    def get_all_snapshots(self) -> dict[int, dict]:
        return {uid: inst.get_state_snapshot() for uid, inst in self._instances.items()}


class MultiRunner:
    _instance: Optional["MultiRunner"] = None

    def __init__(self):
        self._instances: dict[int, HeadlessTwitchInstance] = {}
        self._lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MultiRunner._instance = self

    @classmethod
    def get_instance(cls) -> "MultiRunner":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start_instance(self, user_id: int, user_dir: Path, settings_override: dict | None = None) -> bool:
        async with self._lock:
            # Always remove any existing instance
            if user_id in self._instances:
                old_inst = self._instances[user_id]
                old_inst._process = None
                del self._instances[user_id]
            
            inst = HeadlessTwitchInstance(user_id, user_dir, settings_override)
            self._instances[user_id] = inst
            await inst.start()
            return True

    async def stop_instance(self, user_id: int) -> bool:
        inst = self._instances.get(user_id)
        if inst is None:
            return False
        await inst.stop()
        del self._instances[user_id]
        return True

    async def stop_all(self):
        async with self._lock:
            for user_id in list(self._instances.keys()):
                inst = self._instances[user_id]
                inst._process = None
            self._instances.clear()

    def get_instance_status(self, user_id: int) -> Optional[InstanceStatus]:
        inst = self._instances.get(user_id)
        if inst is None:
            return None
        return inst._status

    def get_instance_snapshot(self, user_id: int) -> dict:
        inst = self._instances.get(user_id)
        if inst is None:
            return {"user_id": user_id, "running": False, "state": "stopped"}
        return inst.get_state_snapshot()

    def get_all_snapshots(self) -> dict[int, dict]:
        return {uid: inst.get_state_snapshot() for uid, inst in self._instances.items()}

    def get_all_status(self) -> list[InstanceStatus]:
        return [inst._status for inst in self._instances.values()]

    async def update_user_settings(self, user_id: int, settings: dict) -> bool:
        inst = self._instances.get(user_id)
        if inst is None:
            return False
        user_settings_path = inst.user_dir / "settings.json"
        current = {}
        if user_settings_path.exists():
            try:
                with open(user_settings_path) as f:
                    current = json.load(f)
            except (json.JSONDecodeError, ValueError):
                current = {}
        current.update(settings)
        with open(user_settings_path, "w") as f:
            json.dump(current, f, indent=2)
        return True

    async def start_monitoring(self):
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self):
        while True:
            await asyncio.sleep(30)
            for inst in list(self._instances.values()):
                if not inst._status.running and not inst._shutdown_event.is_set():
                    state = str(inst._status.state) if inst._status.state else ""
                    # Only restart if it's a recoverable error, not save() issues or GUI errors
                    if "save" not in state.lower() and "icon" not in state.lower() and "error" in state:
                        logger.warning(f"Instance {inst.user_id} died ({state}), attempting restart")
                        await inst.start()
                    elif "save" in state.lower():
                        logger.error(f"Instance {inst.user_id} has save() error - will not auto-restart")

    async def shutdown(self):
        if self._monitor_task:
            self._monitor_task.cancel()
        await self.stop_all()