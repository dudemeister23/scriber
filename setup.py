"""py2app build configuration for Scriber."""

import sys
# py2app's AST scanner can hit the default recursion limit when scanning
# large dependency trees (e.g. mlx_lm, transformers).
sys.setrecursionlimit(5000)

import atexit
import compileall
import importlib.util
import py_compile
import shutil
import zipfile
from pathlib import Path
from setuptools import setup

# Locate libportaudio.dylib from the installed _sounddevice_data package
_sd_spec = importlib.util.find_spec("_sounddevice_data")
_portaudio = str(Path(_sd_spec.submodule_search_locations[0]) / "portaudio-binaries" / "libportaudio.dylib")

# Locate mlx native files (libmlx.dylib + mlx.metallib)
_mlx_spec = importlib.util.find_spec("mlx")
_mlx_lib_dir = Path(_mlx_spec.submodule_search_locations[0]) / "lib"
_libmlx = str(_mlx_lib_dir / "libmlx.dylib")
_mlx_metallib = str(_mlx_lib_dir / "mlx.metallib")


def _post_build_copy_mlx_assets():
    """Copy mlx native assets and Python modules into the bundle.

    core.so has @loader_path/lib as its rpath, so libmlx.dylib and
    mlx.metallib must live in a 'lib/' directory next to core.so.
    Additionally, py2app compiles .py→.pyc but misses some mlx Python
    modules (like _reprlib_fix) that are needed at runtime.
    """
    mlx_dynload = Path("dist/Scriber.app/Contents/Resources/lib/python3.13/lib-dynload/mlx")
    if not mlx_dynload.exists():
        return
    lib_dir = mlx_dynload / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_mlx_metallib, lib_dir / "mlx.metallib")
    shutil.copy2(_libmlx, lib_dir / "libmlx.dylib")
    # Note: metallib is NOT copied to Frameworks/ — it causes codesign
    # --deep --strict failures since it's not a Mach-O binary. MLX finds
    # it via the lib-dynload/mlx/lib/ path through core.so's rpath.

    # Inject missing mlx Python modules into python313.zip.
    # py2app puts mlx/__init__.pyc and mlx/core.pyc in the zip but
    # misses _reprlib_fix.py which mlx.core imports at init time.
    zip_path = Path("dist/Scriber.app/Contents/Resources/lib/python313.zip")
    if zip_path.exists():
        mlx_src = Path(_mlx_spec.submodule_search_locations[0])
        import tempfile
        with zipfile.ZipFile(str(zip_path), "a") as zf:
            existing = set(zf.namelist())
            for py_file in mlx_src.glob("*.py"):
                pyc_name = f"mlx/{py_file.stem}.pyc"
                if pyc_name not in existing:
                    # Compile to .pyc and add to zip
                    with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tmp:
                        tmp_path = tmp.name
                    try:
                        py_compile.compile(str(py_file), cfile=tmp_path, doraise=True)
                        zf.write(tmp_path, pyc_name)
                    finally:
                        Path(tmp_path).unlink(missing_ok=True)


atexit.register(_post_build_copy_mlx_assets)

APP = ["main.py"]
DATA_FILES = [
    ("icons", [
        "scriber/icons/mic_idle.png",
        "scriber/icons/mic_recording.png",
        "scriber/icons/mic_meeting.png",
    ]),
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/Scriber.icns",
    "plist": {
        "CFBundleName": "Scriber",
        "CFBundleDisplayName": "Scriber",
        "CFBundleIdentifier": "com.scriber.app",
        "CFBundleVersion": "1.6.2",
        "CFBundleShortVersionString": "1.6.2",
        "LSUIElement": True,  # Hide from Dock (menubar-only app)
        "NSMicrophoneUsageDescription": "Scriber needs microphone access to record audio for transcription.",
        "NSAppleEventsUsageDescription": "Scriber needs accessibility access to paste transcribed text.",
    },
    "packages": ["scriber", "_sounddevice_data", "charset_normalizer", "numpy", "certifi", "mlx_audio", "mlx_lm"],
    "includes": [
        "rumps",
        "sounddevice",
        "requests",
        "charset_normalizer",
        "idna",
        "urllib3",
        "AVFoundation",
        "ApplicationServices",
        "websocket",
        "huggingface_hub",
    ],
    "excludes": [
        "torch",
        "torchvision",
        "torchaudio",
        "deepmultilingualpunctuation",
        # File-transcription deps are not bundled (torch alone is ~1.5 GB).
        # The feature only works when running from source with
        # requirements-file-transcribe.txt installed.
        "pyannote",
        "pyannote.audio",
        "pytorch_lightning",
        "lightning",
        "lightning_fabric",
        "torchmetrics",
        "pytorch_metric_learning",
        "speechbrain",
        "asteroid_filterbanks",
    ],
    "frameworks": [
        _portaudio,
        _libmlx,
    ],
}

setup(
    app=APP,
    name="Scriber",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
