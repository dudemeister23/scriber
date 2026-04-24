# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs


block_cipher = None

datas = [
    ("scriber/icons/mic_idle.png", "icons"),
    ("scriber/icons/mic_recording.png", "icons"),
    ("scriber/icons/mic_meeting.png", "icons"),
]
datas += collect_data_files("certifi")

binaries = []
binaries += collect_dynamic_libs("_sounddevice_data")

hiddenimports = [
    "ApplicationServices",
    "AVFoundation",
    "Cocoa",
    "CoreAudio",
    "Foundation",
    "Quartz",
    "charset_normalizer",
    "certifi",
    "idna",
    "numpy",
    "requests",
    "rumps",
    "sounddevice",
    "urllib3",
    "websocket",
]

excludes = [
    "asteroid_filterbanks",
    "deepmultilingualpunctuation",
    "huggingface_hub",
    "lightning",
    "lightning_fabric",
    "mlx",
    "mlx_audio",
    "mlx_lm",
    "pyannote",
    "pyannote.audio",
    "pytorch_lightning",
    "pytorch_metric_learning",
    "speechbrain",
    "torch",
    "torchaudio",
    "torchmetrics",
    "torchvision",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Scriber",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/Scriber.icns",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Scriber",
)
app = BUNDLE(
    coll,
    name="Scriber.app",
    icon="assets/Scriber.icns",
    bundle_identifier="com.scriber.app",
    info_plist={
        "CFBundleName": "Scriber",
        "CFBundleDisplayName": "Scriber",
        "CFBundleIdentifier": "com.scriber.app",
        "CFBundleVersion": "1.6.0",
        "CFBundleShortVersionString": "1.6.0",
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": "Scriber needs microphone access to record audio for transcription.",
        "NSAppleEventsUsageDescription": "Scriber needs accessibility access to paste transcribed text.",
    },
)
