# Scriber

A lightweight macOS menubar app for dictation using ElevenLabs Scribe V2 (batch API).

Press a global hotkey, speak, press again — your transcript is pasted into whatever text field has focus. No streaming, no mangling, no Electron. The API's output is inserted verbatim with a trailing space.

## Build

Requires Python 3.10+ and macOS 13+.

```bash
./build.sh
```

This creates `dist/Scriber.app`. Copy it to `/Applications/` to use it from Spotlight.

```bash
cp -r dist/Scriber.app /Applications/
```

## First Launch

1. Double-click Scriber.app (or launch from Spotlight).
2. You'll be prompted to enter your ElevenLabs API key.
3. macOS will ask for **Microphone** and **Accessibility** permissions — grant both in System Settings → Privacy & Security.

## Usage

- **Default hotkey**: `Cmd + Shift + Space` toggles recording on/off.
- The menubar icon changes to indicate recording state.
- When you stop recording, the audio is sent to ElevenLabs Scribe V2 and the transcript is pasted into the focused text field.
- You can also click the menubar icon and select "Start/Stop Dictation".

## Configuration

Config file: `~/.config/scriber/config.json`

```json
{
  "api_key": "your-elevenlabs-api-key",
  "hotkey": "cmd+shift+space",
  "language": "",
  "keyterms": []
}
```

| Key | Description | Default |
|-----|-------------|---------|
| `api_key` | ElevenLabs API key | _(prompted on first launch)_ |
| `hotkey` | Global hotkey binding | `cmd+shift+space` |
| `language` | ISO-639 language code (empty = auto-detect) | `""` |
| `keyterms` | Up to 100 domain-specific terms for Scribe V2 | `[]` |

### Hotkey format

Modifiers: `cmd`, `shift`, `alt`/`option`, `ctrl`/`control`
Keys: `a`-`z`, `0`-`9`, `space`, `return`, `f1`-`f12`, etc.

Separate with `+`: e.g., `cmd+shift+d`, `ctrl+alt+space`

## Logs

API response metadata is logged to `~/.config/scriber/scriber.log` (one JSON object per line) so you can verify billing and debug issues.

## Permissions

Scriber needs two macOS permissions:

- **Microphone**: To record audio for transcription.
- **Accessibility**: To register the global hotkey and simulate Cmd+V paste.

Grant these in **System Settings → Privacy & Security**.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## License

MIT
