"""Configuration management for Scriber."""

import json
import os
import sys

CONFIG_DIR = os.path.expanduser("~/.config/scriber")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
LOG_FILE = os.path.join(CONFIG_DIR, "scriber.log")

DEFAULT_CONFIG = {
    "api_key": "",
    "hotkey": "control",
    "language": "",
    "keyterms": [],
    "input_device": "",
    "mode": "batch",  # "batch", "streaming", or "local"
    "local_fast_mode": False,  # If True, skips Qwen punctuation model
}


def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config() -> dict:
    ensure_config_dir()
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_FILE, "r") as f:
        stored = json.load(f)
    # Merge with defaults for any missing keys
    merged = {**DEFAULT_CONFIG, **stored}
    return merged


def save_config(config: dict):
    ensure_config_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_api_key(config: dict) -> str:
    return config.get("api_key", "")
