"""Updater for Twitch Drop Miner Multiuser.

Strategy:
  1. Fetch the latest upstream miner (DevilXD/TwitchDropsMiner, dev-build).
  2. Fetch our web layer (Deen0X/twitch-drop-miner-multiuser, master).
  3. Build a staging tree: upstream base + our web/ + deployment files + patched main.py.
  4. Verify the staging tree (compile, miner contract, web boot).
  5. Only if verification passes: backup and apply to the project root.

No external dependencies: uses urllib + zipfile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("TwitchDrops.Update")

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STAGING_DIR = DATA_DIR / "update_staging"
BACKUP_DIR = DATA_DIR / "backups"
META_PATH = DATA_DIR / "update_meta.json"
CONFIG_PATH = DATA_DIR / "update_config.json"

UPSTREAM_REPO = "DevilXD/TwitchDropsMiner"
UPSTREAM_REF = "dev-build"
OUR_REPO = "Deen0X/twitch-drop-miner-multiuser"
OUR_REF = "master"
USER_AGENT = "TwitchMinerMultiUser-Updater"

WEB_DEPS = [
    "fastapi>=0.104.0",
    "uvicorn[standard]>=0.24.0",
    "python-multipart>=0.0.6",
    "itsdangerous>=2.1.0",
    "aiofiles>=23.0.0",
    "jinja2>=3.1.0",
    "argon2-cffi>=23.1.0",
]

OVERLAY_FILES = [
    "Dockerfile", "start.sh", "docker-compose.yml", "requirements-web.txt",
    "_dockeriza.cmd", "_Dockerizar.cmd", "run_web.bat", "run_web_lan.bat",
]

PROGRESS: dict = {
    "active": False, "stage": "", "message": "", "percent": 0,
    "downloaded": 0, "total": 0, "success": None, "up_to_date": False,
    "log": "", "started": "", "finished": "",
}


def _set_progress(**kwargs) -> None:
    PROGRESS.update(kwargs)


def get_progress() -> dict:
    return dict(PROGRESS)

EXCLUDE_TOP_DIRS = {
    "data", "config", "config1", ".git", "__pycache__", "env", ".venv", "node_modules",
    "samplecode", "test", "cache", ".opencode", ".opencode.old", ".antigravity",
}
EXCLUDE_FILES = {
    "settings.json", "cookies.jar", "log.txt", "lock.file", "output.log",
    ".env", ".tcloud.env", ".secret_key", "_web_boot.log",
}

MAIN_ARGS_ANCHOR = '    parser.add_argument("--dump", action="store_true")\n'
MAIN_ARGS_INSERT = (
    '    parser.add_argument("--web", action="store_true", help="Run web server mode")\n'
    '    parser.add_argument("--port", type=int, default=4295, help="Web server port (default: 4295)")\n'
    '    parser.add_argument("--host", default="0.0.0.0", help="Web server host (default: 0.0.0.0)")\n'
)
MAIN_PARSE_ANCHOR = "    args = parser.parse_args(namespace=ParsedArgs())\n"
MAIN_PARSE_INSERT = '''
    if args.web:
        root.destroy()
        del root, parser
        from pathlib import Path
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        from web.auth import UserManager
        um = UserManager()
        if not um.admin_exists():
            admin_pass_file = Path(__file__).parent / "config" / "admin_pass.txt"
            default_pass = admin_pass_file.read_text().strip() if admin_pass_file.exists() else "admin"
            try:
                um.create_admin("admin", default_pass)
            except Exception:
                pass
        from web.app import run
        run(port=args.port, host=args.host)
'''


# ── small helpers ──────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return data
    except Exception:
        pass
    return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _token() -> Optional[str]:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    p = DATA_DIR / ".github_token"
    if p.exists():
        return p.read_text().strip()
    return None


def _http_get(url: str, timeout: int = 60, auth: bool = False,
              label: str | None = None, base: float = 0, span: float = 0,
              expected: int = 0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    tok = _token()
    if tok and auth:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if label is None:
            return resp.read()
        total_header = int(resp.headers.get("Content-Length") or 0)
        total = total_header or expected
        chunks = []
        downloaded = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            downloaded += len(chunk)
            ratio = min(downloaded / total, 0.98) if total else 0.5
            _set_progress(stage="download", message=f"Downloading {label}...",
                          percent=round(base + span * ratio, 1),
                          downloaded=downloaded, total=total_header)
        _set_progress(percent=round(base + span, 1), downloaded=downloaded,
                      total=total_header or downloaded)
        return b"".join(chunks)


def _latest_commit_api(repo: str, ref: str) -> dict:
    raw = _http_get(f"https://api.github.com/repos/{repo}/commits/{ref}", timeout=30, auth=True)
    data = json.loads(raw)
    commit = data.get("commit", {})
    return {
        "sha": data.get("sha", ""),
        "date": (commit.get("committer") or {}).get("date", ""),
        "message": (commit.get("message") or "").splitlines()[0] if commit.get("message") else "",
    }


def _latest_commit_atom(repo: str, ref: str) -> dict:
    raw = _http_get(f"https://github.com/{repo}/commits/{ref}.atom", timeout=30)
    root = ET.fromstring(raw)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", ns)
    if entry is None:
        raise RuntimeError("No entries in atom feed")
    entry_id = entry.findtext("a:id", default="", namespaces=ns)
    title = entry.findtext("a:title", default="", namespaces=ns).strip()
    updated = entry.findtext("a:updated", default="", namespaces=ns)
    return {
        "sha": entry_id.rsplit("/", 1)[-1],
        "date": updated,
        "message": title.splitlines()[0] if title else "",
    }


def _latest_commit(repo: str, ref: str, private: bool = False) -> dict:
    try:
        if _token() or private:
            if not _token():
                raise RuntimeError("private repo requires GITHUB_TOKEN or data/.github_token")
            info = _latest_commit_api(repo, ref)
        else:
            info = _latest_commit_atom(repo, ref)
        info.update({"ok": True, "repo": repo, "ref": ref})
        return info
    except Exception as exc:
        return {"ok": False, "repo": repo, "ref": ref, "error": str(exc)}


def _is_excluded(rel: Path) -> bool:
    parts = rel.parts
    if parts and parts[0] in EXCLUDE_TOP_DIRS:
        return True
    if "__pycache__" in parts:
        return True
    if len(parts) >= 2 and parts[0] == "web" and parts[1] == "cache":
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    if rel.suffix in (".pyc", ".pyo"):
        return True
    return False


def _download_source(repo: str, sha: str, dest_dir: Path, private: bool = False,
                     label: str | None = None, base: float = 0, span: float = 0,
                     expected: int = 0) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "source.zip"
    if private and _token():
        data = _http_get(f"https://api.github.com/repos/{repo}/zipball/{sha}",
                         timeout=180, auth=True, label=label, base=base, span=span,
                         expected=expected)
    else:
        data = _http_get(f"https://codeload.github.com/{repo}/zip/{sha}",
                         timeout=180, label=label, base=base, span=span,
                         expected=expected)
    zip_path.write_bytes(data)
    _set_progress(stage="extract", message=f"Extracting {label or repo}...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest_dir)
    for child in dest_dir.iterdir():
        if child.is_dir() and (child / "main.py").exists():
            return child
    raise RuntimeError(f"Source root not found for {repo}@{sha}")


def _patch_main(main_path: Path) -> bool:
    text = main_path.read_text(encoding="utf-8")
    if 'parser.add_argument("--web"' in text and "from web.app import run" in text:
        return False
    if MAIN_ARGS_ANCHOR not in text:
        raise RuntimeError("main.py: anchor for --dump not found")
    if MAIN_PARSE_ANCHOR not in text:
        raise RuntimeError("main.py: parse_args anchor not found")
    text = text.replace(MAIN_ARGS_ANCHOR, MAIN_ARGS_ANCHOR + MAIN_ARGS_INSERT, 1)
    text = text.replace(MAIN_PARSE_ANCHOR, MAIN_PARSE_ANCHOR + MAIN_PARSE_INSERT, 1)
    main_path.write_text(text, encoding="utf-8")
    return True


def _merge_requirements(req_path: Path, web_req: Path) -> None:
    lines = []
    if web_req.exists():
        lines = [l.strip() for l in web_req.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        lines = WEB_DEPS
    current = req_path.read_text(encoding="utf-8") if req_path.exists() else ""
    lowered = current.lower()
    extra = []
    for line in lines:
        pkg = line.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
        if pkg and pkg not in lowered:
            extra.append(line)
    if extra:
        req_path.write_text(current.rstrip() + "\n\n# web layer\n" + "\n".join(extra) + "\n", encoding="utf-8")


def _build_staging(upstream_sha: str, our_sha: Optional[str]) -> Path:
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
    dl = STAGING_DIR / "dl"
    dl.mkdir(parents=True, exist_ok=True)

    up_root = _download_source(UPSTREAM_REPO, upstream_sha, dl / "upstream",
                               label="miner (upstream)", base=2, span=34,
                               expected=2 * 1024 * 1024)

    overlay_source = PROJECT_ROOT
    if our_sha:
        _set_progress(stage="download", message="Downloading web layer (ours)...", percent=38)
        try:
            overlay_source = _download_source(OUR_REPO, our_sha, dl / "ours", private=True,
                                              label="web layer (ours)", base=38, span=18,
                                              expected=1024 * 1024)
        except Exception as exc:
            logger.warning("Could not fetch our repo (%s); using current web layer", exc)
            overlay_source = PROJECT_ROOT
            _set_progress(message="Web layer unavailable; using current files", percent=58)

    _set_progress(stage="build", message="Building staging tree...", percent=58)
    build = STAGING_DIR / "build"
    shutil.copytree(up_root, build)

    # Overlay our web layer and deployment files.
    if (overlay_source / "web").is_dir():
        shutil.copytree(
            overlay_source / "web", build / "web", dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("cache", "__pycache__", "*.pyc"),
        )
    for name in OVERLAY_FILES:
        src = overlay_source / name
        if src.exists():
            shutil.copy2(src, build / name)

    _patch_main(build / "main.py")

    web_req = overlay_source / "requirements-web.txt"
    _merge_requirements(build / "requirements.txt", web_req)
    return build


def _compile_tree(root: Path) -> tuple[bool, str]:
    import py_compile
    errors = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if _is_excluded(rel):
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            errors.append(f"{rel}: {exc}")
    return (not errors, "\n".join(errors))


CONTRACT_SCRIPT = '''\
import sys
sys.path.insert(0, ".")
from settings import Settings
from twitch import Twitch
from inventory import TimedDrop
from channel import Channel

class A:
    log = False
    tray = False
    dump = False
    _verbose = 0
    debug_ws = 0
    debug_gql = 0
    logging_level = 20

s = Settings(A())
assert hasattr(TimedDrop, "update_minutes"), "TimedDrop.update_minutes missing"
assert hasattr(Channel, "send_watch"), "Channel.send_watch missing"
client = Twitch(s)
print("CONTRACT_OK")
'''


def _py_cmd() -> list[str]:
    cmd = [sys.executable]
    if sys.platform.startswith("linux") and shutil.which("xvfb-run"):
        return ["xvfb-run", "-a"] + cmd
    return cmd


def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return -1, f"TIMEOUT after {timeout}s\n{exc.stdout or ''}\n{exc.stderr or ''}"
    except Exception as exc:
        return -1, str(exc)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _verify_web(build: Path, log: list[str]) -> bool:
    port = _free_port()
    cmd = _py_cmd() + ["main.py", "--web", "--port", str(port), "--host", "127.0.0.1"]
    out_path = build / "_web_boot.log"
    with open(out_path, "w") as out:
        proc = subprocess.Popen(cmd, cwd=str(build), stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.time() + 30
            ok = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2) as resp:
                        ok = resp.status == 200
                        if ok:
                            break
                except Exception:
                    time.sleep(1)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
    text = out_path.read_text(encoding="utf-8", errors="replace") if out_path.exists() else ""
    log.append("WEB BOOT " + ("OK" if ok else "FAILED") + "\n" + text[-4000:])
    return ok


def _verify(build: Path) -> tuple[bool, str, str]:
    log: list[str] = []
    _set_progress(stage="verify", message="Compiling staging...", percent=64)
    ok, err = _compile_tree(build)
    log.append("COMPILE " + ("OK" if ok else "FAILED") + ("\n" + err if err else ""))
    if not ok:
        return False, "\n\n".join(log), "compile"

    _set_progress(stage="verify", message="Checking miner contract...", percent=71)
    script = build / "_verify_contract.py"
    script.write_text(CONTRACT_SCRIPT, encoding="utf-8")
    code, out = _run(_py_cmd() + [str(script)], build, timeout=60)
    script.unlink(missing_ok=True)
    log.append(f"CONTRACT rc={code}\n{out[-4000:]}")
    if code != 0 or "CONTRACT_OK" not in out:
        return False, "\n\n".join(log), "contract"

    _set_progress(stage="verify", message="Booting web in staging...", percent=80)
    if not _verify_web(build, log):
        return False, "\n\n".join(log), "web"
    _set_progress(stage="verify", message="Staging verification OK", percent=92)
    return True, "\n\n".join(log), "ok"


def _backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"update_{stamp}.zip"

    items = []
    for item in PROJECT_ROOT.rglob("*"):
        rel = item.relative_to(PROJECT_ROOT)
        if item.is_file() and not _is_excluded(rel):
            items.append((item, rel))
    total = len(items)
    _set_progress(stage="apply", message=f"Backing up {total} files...", percent=92)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for index, (item, rel) in enumerate(items, 1):
            z.write(item, str(rel))
            if index % 20 == 0 or index == total:
                _set_progress(stage="apply", message=f"Backing up {index}/{total} files...",
                              percent=round(92 + 4 * index / max(total, 1), 1))
    return path


def _sync_tree(src: Path, dst: Path) -> list[str]:
    skipped: list[str] = []
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if _is_excluded(rel):
            continue
        target = dst / rel
        try:
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        except OSError as exc:
            skipped.append(f"{rel}: {exc}")
            logger.warning("Skipping locked file %s: %s", rel, exc)
    return skipped


# ── public API ─────────────────────────────────────────────────────────

def check_updates_sync() -> dict:
    meta = _load_json(META_PATH, {})
    upstream = _latest_commit(UPSTREAM_REPO, UPSTREAM_REF)
    ours = _latest_commit(OUR_REPO, OUR_REF, private=True)
    for item in (upstream, ours):
        item["applied_sha"] = meta.get("upstream_sha") if item is upstream else meta.get("our_sha")
        item["update_available"] = bool(item.get("ok")) and item.get("sha") != item.get("applied_sha")
    return {
        "upstream": upstream,
        "ours": ours,
        "applied_at": meta.get("applied_at"),
        "config": get_update_config(),
    }


def apply_update_sync(dry_run: bool = False) -> dict:
    now = lambda: datetime.now(timezone.utc).isoformat()
    _set_progress(active=True, success=None, up_to_date=False, stage="check",
                  message="Checking GitHub...", percent=1, downloaded=0, total=0,
                  log="", started=now(), finished="")

    check = check_updates_sync()
    upstream, ours = check["upstream"], check["ours"]
    if not upstream.get("ok"):
        msg = "Upstream check failed: " + str(upstream.get("error"))
        _set_progress(active=False, success=False, stage="check", message=msg, finished=now())
        return {"success": False, "stage": "check", "message": msg, "detail": check}
    if not dry_run and not (upstream["update_available"] or ours.get("update_available")):
        _set_progress(active=False, success=True, up_to_date=True, stage="done",
                      message="Already up to date", percent=100, finished=now())
        return {"success": True, "up_to_date": True, "message": "Already up to date"}
    try:
        build = _build_staging(upstream["sha"], ours.get("sha") if ours.get("ok") else None)
    except Exception as exc:
        logger.exception("Staging build failed")
        _set_progress(active=False, success=False, stage="build", message=str(exc), finished=now())
        return {"success": False, "stage": "build", "message": str(exc)}
    ok, log, stage = _verify(build)
    if not ok:
        _set_progress(active=False, success=False, stage=stage,
                      message="Staging verification failed", log=log, percent=100, finished=now())
        return {"success": False, "stage": stage, "message": "Staging verification failed", "log": log}
    if dry_run:
        _set_progress(active=False, success=True, stage="done", message="Staging OK",
                      log=log, percent=100, finished=now())
        return {"success": True, "dry_run": True, "message": "Staging OK", "log": log,
                "staging": str(build)}
    try:
        _set_progress(stage="apply", message="Backing up current code...", percent=94)
        backup = _backup()
        _set_progress(stage="apply", message="Applying update...", percent=97)
        skipped = _sync_tree(build, PROJECT_ROOT)
        _write_json(META_PATH, {
            "upstream_sha": upstream["sha"],
            "upstream_date": upstream.get("date", ""),
            "our_sha": ours.get("sha") or "",
            "our_date": ours.get("date", ""),
            "applied_at": now(),
            "backup": str(backup),
            "skipped": skipped,
        })
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
    except Exception as exc:
        logger.exception("Apply failed")
        _set_progress(active=False, success=False, stage="apply", message=str(exc), finished=now())
        return {"success": False, "stage": "apply", "message": str(exc)}
    _set_progress(active=False, success=True, stage="done", message="Update applied",
                  percent=100, log=log, finished=now())
    return {"success": True, "message": "Update applied", "backup": str(backup), "log": log}


async def check_updates() -> dict:
    return await asyncio.to_thread(check_updates_sync)


async def apply_update(dry_run: bool = False) -> dict:
    return await asyncio.to_thread(apply_update_sync, dry_run)


def get_update_config() -> dict:
    cfg = _load_json(CONFIG_PATH, {})
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "hour": int(cfg.get("hour", 4)),
        "last_check": cfg.get("last_check", ""),
        "last_result": cfg.get("last_result", ""),
    }


def set_update_config(**kwargs) -> dict:
    cfg = get_update_config()
    for key in ("enabled", "hour", "last_check", "last_result"):
        if key in kwargs:
            cfg[key] = kwargs[key]
    _write_json(CONFIG_PATH, cfg)
    return cfg
