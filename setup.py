"""py2app build configuration for Scriber."""

import importlib.util
from pathlib import Path
from setuptools import setup

# Locate libportaudio.dylib from the installed _sounddevice_data package
_sd_spec = importlib.util.find_spec("_sounddevice_data")
_portaudio = str(Path(_sd_spec.submodule_search_locations[0]) / "portaudio-binaries" / "libportaudio.dylib")

APP = ["main.py"]
DATA_FILES = [
    ("icons", [
        "scriber/icons/mic_idle.png",
        "scriber/icons/mic_recording.png",
    ]),
]
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/Scriber.icns",
    "plist": {
        "CFBundleName": "Scriber",
        "CFBundleDisplayName": "Scriber",
        "CFBundleIdentifier": "com.scriber.app",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # Hide from Dock (menubar-only app)
        "NSMicrophoneUsageDescription": "Scriber needs microphone access to record audio for transcription.",
        "NSAppleEventsUsageDescription": "Scriber needs accessibility access to paste transcribed text.",
    },
    "packages": ["scriber", "_sounddevice_data", "charset_normalizer", "numpy"],
    "includes": [
        "rumps",
        "sounddevice",
        "requests",
        "certifi",
        "charset_normalizer",
        "idna",
        "urllib3",
        "AVFoundation",
        "ApplicationServices",
        "websocket",
    ],
    "frameworks": [
        _portaudio,
    ],
}

setup(
    app=APP,
    name="Scriber",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
