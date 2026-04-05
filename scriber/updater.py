"""Check for and install app updates from GitHub releases.

The flow:
  1. Fetch the latest release from the GitHub API.
  2. Compare its tag (e.g. "v1.4.0") to the running __version__.
  3. If newer and the release has a .zip asset, stream-download it,
     extract with `ditto`, then detach a small bash helper that waits
     for this process to exit, swaps the .app bundle, and relaunches.
  4. If there's no .zip asset, fall back to opening the release page.

Dev mode (running from source via `python main.py`) is detected by the
absence of a .app bundle in sys.executable — installation is disabled
there but checking is still allowed.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import requests

logger = logging.getLogger("scriber")

GITHUB_API_URL = "https://api.github.com/repos/dudemeister23/scriber/releases/latest"
REQUEST_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 60.0
CHUNK_SIZE = 1 << 16  # 64 KiB


@dataclass
class ReleaseInfo:
    version: str               # normalized, no "v" prefix (e.g. "1.4.0")
    tag_name: str              # raw tag (e.g. "v1.4.0")
    name: str                  # release display name
    notes: str                 # release body (markdown)
    html_url: str              # release page on GitHub
    asset_url: Optional[str]   # direct download URL for .zip, or None
    asset_name: Optional[str]  # e.g. "Scriber.app.zip"
    asset_size: Optional[int]  # bytes


def _parse_version(v: str) -> tuple:
    """Parse "v1.3.0" → (1, 3, 0). Pre-release suffixes are stripped."""
    s = v.lstrip("vV").strip()
    base = s.split("-", 1)[0]
    parts = []
    for p in base.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(remote: str, current: str) -> bool:
    """True if `remote` is strictly newer than `current`."""
    return _parse_version(remote) > _parse_version(current)


def get_latest_release() -> Optional[dict]:
    """Fetch latest release JSON from GitHub. Returns None on failure."""
    try:
        resp = requests.get(
            GITHUB_API_URL,
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Update check failed: %s", e)
        return None


def check_for_update(current_version: str) -> Optional[ReleaseInfo]:
    """Return a ReleaseInfo if a newer release exists, else None."""
    data = get_latest_release()
    if not data:
        return None
    tag = data.get("tag_name") or ""
    if not tag or not is_newer(tag, current_version):
        return None

    # Prefer a .zip asset over a .dmg — zip can be extracted and swapped
    # non-interactively; dmg requires mounting.
    asset_url = asset_name = None
    asset_size = None
    for asset in data.get("assets") or []:
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip"):
            asset_url = asset.get("browser_download_url")
            asset_name = asset.get("name")
            asset_size = asset.get("size")
            break

    return ReleaseInfo(
        version=tag.lstrip("vV"),
        tag_name=tag,
        name=data.get("name") or tag,
        notes=data.get("body") or "",
        html_url=data.get("html_url") or "",
        asset_url=asset_url,
        asset_name=asset_name,
        asset_size=asset_size,
    )


# --- Bundle detection ---


def is_running_from_bundle() -> bool:
    """True when launched from a .app bundle (py2app), False in dev mode."""
    if os.environ.get("RESOURCEPATH"):
        return True
    return ".app/Contents/MacOS/" in sys.executable


def get_bundle_path() -> Optional[str]:
    """Absolute path to the running .app bundle, or None in dev mode."""
    if not is_running_from_bundle():
        return None
    exe = os.path.realpath(sys.executable)
    parts = exe.split(os.sep)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].endswith(".app"):
            return os.sep.join(parts[: i + 1])
    return None


# --- Download & install ---


def _updates_dir() -> str:
    d = os.path.expanduser("~/Library/Application Support/Scriber/updates")
    os.makedirs(d, exist_ok=True)
    return d


def download_release(
    url: str,
    dest_path: str,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Stream-download `url` to `dest_path`, calling progress_callback(received, total)."""
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        received = 0
        tmp_path = dest_path + ".part"
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("Download cancelled")
                if chunk:
                    f.write(chunk)
                    received += len(chunk)
                    if progress_callback:
                        progress_callback(received, total)
        os.replace(tmp_path, dest_path)


def _sh_quote(s: str) -> str:
    """Shell-quote a path for embedding in a bash script."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def stage_and_install(zip_path: str, current_bundle_path: str) -> None:
    """Extract the zip, write an install script, and detach it.

    The install script waits for the current process to exit, then swaps
    the .app bundle and relaunches. The caller should quit immediately
    after this function returns so the script can proceed.
    """
    updates = _updates_dir()
    staging = os.path.join(updates, "staging")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    # `ditto -x -k` extracts a zip while preserving symlinks, xattrs,
    # and code-signing metadata — `zipfile` doesn't preserve those.
    result = subprocess.run(
        ["ditto", "-x", "-k", zip_path, staging],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ditto failed: {result.stderr.strip()}")

    # Locate the .app inside the extracted tree (may be at any depth).
    new_app = _find_app_bundle(staging)
    if new_app is None:
        raise RuntimeError("No .app bundle found inside the downloaded zip")

    log_path = os.path.join(updates, "install.log")
    script_path = os.path.join(updates, "install.sh")
    pid = os.getpid()

    script = f"""#!/bin/bash
# Scriber auto-updater install script.
# Waits for Scriber (pid {pid}) to exit, then swaps the bundle and relaunches.
exec >{_sh_quote(log_path)} 2>&1
set -x

for i in $(seq 1 100); do
    if ! kill -0 {pid} 2>/dev/null; then
        break
    fi
    sleep 0.1
done
# Extra beat so file handles release cleanly
sleep 0.5

# Strip quarantine from the freshly downloaded bundle
xattr -dr com.apple.quarantine {_sh_quote(new_app)} 2>/dev/null || true

# Swap the bundle
rm -rf {_sh_quote(current_bundle_path)}
mv {_sh_quote(new_app)} {_sh_quote(current_bundle_path)}

# Relaunch
open {_sh_quote(current_bundle_path)}
"""
    with open(script_path, "w") as f:
        f.write(script)
    os.chmod(script_path, 0o755)

    subprocess.Popen(
        ["/bin/bash", script_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    logger.info("Install script detached; see %s", log_path)


def _find_app_bundle(root: str) -> Optional[str]:
    """Find the first .app directory within `root` (BFS, depth ≤ 3)."""
    queue = [(root, 0)]
    while queue:
        path, depth = queue.pop(0)
        if depth > 3:
            continue
        try:
            entries = os.listdir(path)
        except OSError:
            continue
        for name in entries:
            full = os.path.join(path, name)
            if name.endswith(".app") and os.path.isdir(full):
                return full
            if os.path.isdir(full) and not os.path.islink(full):
                queue.append((full, depth + 1))
    return None


def format_notes(notes: str, limit: int = 400) -> str:
    """Truncate release notes for display in a small alert dialog."""
    s = (notes or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "\u2026"
