#!/usr/bin/env python3
"""Entry point for Scriber."""

import logging
import os
import warnings

warnings.filterwarnings("ignore", message=".*character detection.*")

from scriber.config import CONFIG_DIR, ensure_config_dir

# Set up file + console logging before importing anything else
ensure_config_dir()
log_path = os.path.join(CONFIG_DIR, "app.log")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path, mode="w"),  # overwrite each launch
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("scriber")
logger.info("Scriber starting, log file: %s", log_path)

from scriber.app import ScribeApp
from scriber.hotkey import GlobalHotkey


def run():
    app = ScribeApp()
    logger.info("Config: %s", {k: v for k, v in app.config.items() if k != "api_key"})

    # Set up global hotkey
    hotkey_str = app.config.get("hotkey", "control")
    try:
        hotkey = GlobalHotkey(
            hotkey_str,
            on_press=app.start_recording,
            on_release=app.stop_and_transcribe,
            on_cancel=app.cancel_recording,
        )
        hotkey.start()
        logger.info("Global hotkey registered: %s", hotkey_str)
    except Exception as e:
        logger.error("Failed to register hotkey '%s': %s", hotkey_str, e)

    app.run()


if __name__ == "__main__":
    run()
