"""On-demand install of heavy dependencies for file transcription.

The file-transcription feature needs ~1.5 GB of extras (pyannote.audio, torch,
torchaudio) that are deliberately NOT bundled with Scriber.app. This module:

1. Points `sys.path` at a persistent external install directory at app
   startup (so previously-installed deps are found).
2. Installs those deps on demand via pip when the user first uses the
   feature, writing to that same directory.

The install directory lives outside the .app bundle so it survives app
updates and rebuilds.
"""

import logging
import os
import platform
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger("scriber")

# Deps live here, versioned by Python minor version because compiled
# extensions (.so) are tied to a specific Python X.Y.
_PY_TAG = f"py{sys.version_info.major}{sys.version_info.minor}"
DEPS_DIR = os.path.expanduser(
    f"~/Library/Application Support/Scriber/python-deps/{_PY_TAG}"
)

# Packages to install for the file-transcription feature.
# - pyannote.audio 4.x uses the modern huggingface_hub API (`token=` instead
#   of removed `use_auth_token=`) and no longer references the removed
#   `torchaudio.AudioMetaData`.
# - pyannote.audio 4.x requires torch>=2.8.
FILE_TRANSCRIBE_PACKAGES = [
    "pyannote.audio>=4.0.0,<5.0.0",
    "torch>=2.8.0",
    "torchaudio>=2.8.0",
]

# Human-readable names for each required import.
REQUIRED_IMPORTS = ("torch", "pyannote.audio")


def inject_deps_path() -> None:
    """Prepend DEPS_DIR to sys.path if it exists.

    Safe to call multiple times. Call this BEFORE attempting to import
    torch or pyannote in this process.
    """
    if os.path.isdir(DEPS_DIR) and DEPS_DIR not in sys.path:
        sys.path.insert(0, DEPS_DIR)
        logger.info("Injected file-transcribe deps path: %s", DEPS_DIR)


def are_deps_installed() -> bool:
    """Return True if the required packages are on disk and importable.

    Uses importlib.util.find_spec rather than __import__ so this check is
    fast (no torch load) and doesn't blow up if a partial/broken install
    exists on disk. A broken install (e.g. torchaudio version mismatch)
    will still show up as 'installed' — the real import failure will surface
    later when the pipeline actually runs.
    """
    import importlib.util
    inject_deps_path()
    for mod in REQUIRED_IMPORTS:
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ValueError, AttributeError, ModuleNotFoundError):
            return False
        if spec is None:
            return False
    return True


def _find_installer_python() -> str:
    """Locate a pip-capable Python binary matching this Python's X.Y version.

    Preference order:
        1. sys.executable (dev mode: the running .venv/bin/python has pip)
        2. /Library/Frameworks/Python.framework/Versions/X.Y/bin/python3.Y
        3. /opt/homebrew/bin/python3.Y or /usr/local/bin/python3.Y
        4. `python3.Y` on PATH

    Returns the binary path. Raises RuntimeError if no matching Python found.
    """
    major, minor = sys.version_info.major, sys.version_info.minor
    candidates = []

    # 1. sys.executable — works in dev mode (.venv python has pip)
    if sys.executable and "Scriber.app" not in sys.executable:
        candidates.append(sys.executable)

    # 2. System framework Python (python.org)
    candidates.append(
        f"/Library/Frameworks/Python.framework/Versions/{major}.{minor}"
        f"/bin/python{major}.{minor}"
    )

    # 3. Homebrew paths
    candidates.extend([
        f"/opt/homebrew/bin/python{major}.{minor}",
        f"/opt/homebrew/bin/python3",
        f"/usr/local/bin/python{major}.{minor}",
        f"/usr/local/bin/python3",
    ])

    # 4. PATH
    on_path = shutil.which(f"python{major}.{minor}") or shutil.which("python3")
    if on_path:
        candidates.append(on_path)

    # De-duplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c and c not in seen:
            unique.append(c)
            seen.add(c)

    for candidate in unique:
        if not os.path.isfile(candidate):
            continue
        if not _python_has_matching_version_and_pip(candidate, major, minor):
            continue
        logger.info("Using %s to install file-transcribe deps", candidate)
        return candidate

    raise RuntimeError(
        f"No Python {major}.{minor} with pip found. Install python.org "
        f"Python {major}.{minor} from https://www.python.org/downloads/"
    )


def _python_has_matching_version_and_pip(python_bin: str, major: int, minor: int) -> bool:
    """Check that the given Python is X.Y and has pip importable."""
    try:
        check = subprocess.run(
            [python_bin, "-c",
             f"import sys, pip; assert sys.version_info[:2] == ({major}, {minor}); print('ok')"],
            capture_output=True, text=True, timeout=10,
        )
        return check.returncode == 0 and "ok" in check.stdout
    except (subprocess.SubprocessError, OSError) as e:
        logger.debug("Python candidate %s failed check: %s", python_bin, e)
        return False


def install_deps(on_progress=None, on_complete=None, on_error=None,
                 cancel_event: threading.Event = None,
                 clean: bool = False):
    """Install file-transcribe packages into DEPS_DIR via pip.

    Runs in a background thread. Streams pip output to on_progress.

    Args:
        on_progress: called with str status updates (thread-safe caller's job)
        on_complete: called when install finishes successfully
        on_error: called with an error message string on failure
        cancel_event: threading.Event to abort the install
        clean: if True, wipe DEPS_DIR first (for reinstalls / version fixes)

    Returns the Thread.
    """
    def _worker():
        proc = None
        try:
            if clean and os.path.isdir(DEPS_DIR):
                if on_progress:
                    on_progress("Removing previous install\u2026")
                shutil.rmtree(DEPS_DIR)
            os.makedirs(DEPS_DIR, exist_ok=True)

            if on_progress:
                on_progress("Locating Python installer\u2026")
            python_bin = _find_installer_python()

            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Cancelled")

            if on_progress:
                on_progress("Starting pip install (this may take several minutes)\u2026")

            cmd = [
                python_bin, "-m", "pip", "install",
                "--target", DEPS_DIR,
                "--upgrade",
                "--disable-pip-version-check",
                "--progress-bar", "off",
                *FILE_TRANSCRIBE_PACKAGES,
            ]
            logger.info("pip install cmd: %s", " ".join(cmd))

            # Strip CA-cert env vars that may point INSIDE DEPS_DIR (e.g. from
            # a previous install's certifi, now wiped). Let pip use its own
            # bundled certifi.
            env = os.environ.copy()
            for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                if var in env and DEPS_DIR in env[var]:
                    env.pop(var, None)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered
                env=env,
            )

            for raw_line in iter(proc.stdout.readline, ""):
                if cancel_event is not None and cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise RuntimeError("Cancelled")

                line = raw_line.rstrip()
                if not line:
                    continue
                logger.debug("pip: %s", line)

                # Surface interesting lines as progress updates
                if on_progress:
                    lower = line.lower()
                    if lower.startswith("collecting "):
                        pkg = line.split(None, 1)[1].split()[0]
                        on_progress(f"Downloading {pkg}\u2026")
                    elif lower.startswith("downloading "):
                        # e.g. "Downloading torch-2.3.0-cp313-..."
                        parts = line.split()
                        if len(parts) > 1:
                            on_progress(f"Downloading {parts[1]}\u2026")
                    elif lower.startswith("installing collected"):
                        on_progress("Installing packages\u2026")
                    elif lower.startswith("successfully installed"):
                        on_progress("Install complete")

            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"pip install failed with exit code {proc.returncode} "
                    f"(see Scriber log for details)"
                )

            inject_deps_path()
            logger.info("File-transcribe deps installed at %s", DEPS_DIR)
            if on_complete:
                on_complete()

        except Exception as e:
            # Best-effort cleanup of the running process
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            import traceback
            logger.error("Deps install failed: %s\n%s", e, traceback.format_exc())
            if on_error:
                on_error(str(e))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


def uninstall_deps():
    """Remove the entire DEPS_DIR. Use to clean up a bad install."""
    if os.path.isdir(DEPS_DIR):
        shutil.rmtree(DEPS_DIR)
        logger.info("Removed deps dir: %s", DEPS_DIR)


def deps_dir_size_mb() -> float:
    """Return the on-disk size of DEPS_DIR in megabytes, or 0 if missing."""
    if not os.path.isdir(DEPS_DIR):
        return 0.0
    total = 0
    for root, _, files in os.walk(DEPS_DIR):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024 * 1024)
