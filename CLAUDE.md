# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project
Scriber — a macOS menubar dictation app. Press a global hotkey, speak, release → transcript is pasted into the focused text field. Modes: batch (ElevenLabs Scribe V2), streaming (Scribe V2 RT), and local on-device (IBM Granite 4.0 1B).

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
- `scriber/overlay.py` — Floating HUD above the dock (waveform + status).
- `scriber/hotkey.py` — Global hotkey registration via Quartz event tap.
- `scriber/paste.py` — Cmd+V simulation with clipboard save/restore.
- `scriber/updater.py` — GitHub release check, download, and install-script generation.
- `scriber/settings_window.py` — AppKit settings window (API key, hotkey, mode, local model).

## Thread safety
Background threads (transcription, update check/download, websocket recv) must marshal UI work to the main thread. Use the `_MainThreadDispatcher` NSObject on `ScribeApp` (calls `performSelectorOnMainThread_`) — **do not** use `rumps.Timer` from a background thread: it attaches to the calling thread's run loop, which doesn't exist in plain Python threads, so the timer never fires.
