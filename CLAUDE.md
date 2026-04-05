# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project
Scriber — a macOS menubar dictation app. Press a global hotkey, speak, release → transcript is pasted into the focused text field. Modes: batch (ElevenLabs Scribe V2), streaming (Scribe V2 RT), and local on-device (IBM Granite 4.0 1B).

There is also a separate **File Transcription** feature (menu: "Transcribe File…") for offline diarized transcription of pre-recorded audio/video files. Pipeline: ffmpeg decode → pyannote 3.1 diarization → Parakeet ASR per segment → merge same-speaker runs. Fully offline after model download; no API calls. See `scriber/file_transcribe.py` and `scriber/file_transcribe_window.py`.

There is also a **Meeting Recording** feature (menu: "Start Meeting Recording") that captures the user's mic **+** all system audio output into a single mixed WAV under `~/Documents/Scriber Meetings/`, then drops the user into the File Transcription window with the file pre-loaded. System audio is captured via Core Audio Process Taps (macOS 14.4+) so it only triggers the orange mic indicator, not the purple Screen Recording one — designed to stay subtle during screen-shares. Menu-bar icon is a near-identical variant of `mic_idle` with a 2-px corner dot while recording. See `scriber/system_audio.py` and `scriber/meeting_recorder.py`.

## Build
```bash
./build.sh
```
Produces `dist/Scriber.app` (code-signed with hardened runtime + entitlements so TCC permissions persist across rebuilds). The build signs with the identity in `$SCRIBER_SIGNING_IDENTITY` (defaults to the user's Apple Developer cert).

Dev run (unsigned, from source):
```bash
source .venv/bin/activate
python main.py
```

### File transcription deps (on-demand, ~1.5 GB)
The file-transcription feature needs heavy deps (pyannote.audio + torch) that are **intentionally excluded from the py2app bundle** to keep `Scriber.app` small. They are also NOT in `requirements.txt` (would bloat every rebuild).

Instead, they're installed **on first use** via `scriber/deps_manager.py`:
- First time the user clicks "Start" in the file-transcription window, an NSAlert asks whether to install.
- If they confirm, pip installs the packages to `~/Library/Application Support/Scriber/python-deps/py{X}{Y}/` (versioned by Python X.Y since compiled extensions are version-specific).
- That directory is prepended to `sys.path` at app startup (in `main.py`) so subsequent launches find the deps.
- The directory lives outside the `.app`, so it survives both app updates and rebuilds.
- On-demand install uses `subprocess` calling a detected pip-capable Python: prefers `sys.executable` when running from source; falls back to `/Library/Frameworks/Python.framework/Versions/X.Y/bin/pythonX.Y` when running from the bundle.

The entitlement `com.apple.security.cs.disable-library-validation` is already set in `Scriber.entitlements`, which is required for loading the unsigned `.dylib` files that come inside the torch wheels.

Also requires ffmpeg (system binary): `brew install ffmpeg`, and a HuggingFace token with EULA accepted at https://huggingface.co/pyannote/speaker-diarization-3.1 — paste into Settings, then click "Download Diarization Model".

**Dev-mode alternative:** If you'd rather install into your `.venv` directly (e.g. to avoid the app-managed directory during development):
```bash
source .venv/bin/activate
pip install -r requirements-file-transcribe.txt
```
`deps_manager.inject_deps_path()` is a no-op when the deps dir doesn't exist, so this coexists cleanly.

**To reset the install** (e.g. after a bad/partial install): `rm -rf ~/Library/Application\ Support/Scriber/python-deps/`.

## Versioning
`__version__` lives in `scriber/__init__.py`. Bump it on every release — the in-app auto-updater reads this to compare against the latest GitHub release tag.

## Releases — MUST include the zip asset

Every GitHub release MUST have a `Scriber.app.zip` asset attached. The in-app auto-updater downloads this asset, extracts it with `ditto`, and swaps the running `.app`. Releases without a zip asset leave users on the old version with only a "View on GitHub" button.

**Release checklist:**
1. Bump `__version__` in `scriber/__init__.py`.
2. Commit the changes.
3. Run `./build.sh` to produce a signed `dist/Scriber.app`.
4. Create the zip asset (preserves symlinks + xattrs + code signing):
   ```bash
   ditto -c -k --sequesterRsrc --keepParent dist/Scriber.app Scriber.app.zip
   ```
5. Push to `main`, then create the release + upload the zip:
   ```bash
   gh release create vX.Y.Z Scriber.app.zip \
     --title "vX.Y.Z — <short summary>" \
     --notes "<release notes>"
   ```
6. Verify on the releases page that the `Scriber.app.zip` asset is attached.

Tags use the `vX.Y.Z` format (e.g. `v1.4.0`). The updater strips the `v` prefix when comparing to `__version__`.

## Architecture quick-reference
- `scriber/app.py` — `ScribeApp` (rumps menubar app). Owns recording lifecycle, mode dispatch, settings window, updater flow.
- `scriber/audio.py` — `AudioRecorder` (sounddevice-based capture).
- `scriber/transcribe.py` — ElevenLabs Scribe V2 batch API.
- `scriber/streaming.py` — ElevenLabs Scribe V2 RT websocket client.
- `scriber/local_transcribe.py` — On-device transcription via MLX (Granite + Qwen punctuation).
- `scriber/file_transcribe.py` — Offline diarization + ASR pipeline (pyannote + Parakeet) for file transcription. Lazy-imports torch/pyannote so the module loads even without those deps installed.
- `scriber/file_transcribe_window.py` — AppKit window for the file-transcription flow (file picker, speaker count, progress, results, Copy/Save TXT/Save SRT). Prompts to install missing deps on first use via `deps_manager`.
- `scriber/deps_manager.py` — On-demand installer for pyannote.audio + torch. Maintains `~/Library/Application Support/Scriber/python-deps/py{X}{Y}/` and prepends it to `sys.path` at startup.
- `scriber/system_audio.py` — Core Audio Process Tap wrapper (macOS 14.4+) that captures system audio output as mono 16 kHz PCM. Uses pyobjc for CATapDescription + aggregate device, ctypes for the real-time IOProc (pyobjc can't round-trip the opaque AudioDeviceIOProcID pointer).
- `scriber/meeting_recorder.py` — Coordinates `AudioRecorder` (mic) + `SystemAudioTap` (system audio), mixes both at 0.7 gain, streams to a WAV file in `~/Documents/Scriber Meetings/`. Runs a background mixer thread that drains both sources every 500 ms for bounded memory on long meetings.
- `scriber/overlay.py` — Floating HUD above the dock (waveform + status).
- `scriber/hotkey.py` — Global hotkey registration via Quartz event tap.
- `scriber/paste.py` — Cmd+V simulation with clipboard save/restore.
- `scriber/updater.py` — GitHub release check, download, and install-script generation.
- `scriber/settings_window.py` — AppKit settings window (API key, hotkey, mode, local model, HF token + diarization model download).

## Thread safety
Background threads (transcription, update check/download, websocket recv) must marshal UI work to the main thread. Use the `_MainThreadDispatcher` NSObject on `ScribeApp` (calls `performSelectorOnMainThread_`) — **do not** use `rumps.Timer` from a background thread: it attaches to the calling thread's run loop, which doesn't exist in plain Python threads, so the timer never fires.
