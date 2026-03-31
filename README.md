# Jitsi Audio Recording Bot

Headless Python bot that joins a Jitsi room and records remote audio to WAV.

## Requirements

- Python 3.9+
- Dependencies from `requirements.txt`

## Install

```bash
python3.9 -m pip install -r requirements.txt
```

## Basic Usage

```bash
/opt/homebrew/bin/python3.9 bot.py \
  --host meet.jit.si \
  --room testroom-bot-xyz123 \
  --duration 20 \
  --output ~/rec.wav
```

`--server` is supported as an alias of `--host`.

## Self-Hosted Jitsi Usage

Use explicit domains when your deployment differs from defaults:

```bash
/opt/homebrew/bin/python3.9 bot.py \
  --host meet.example.com \
  --xmpp-domain guest.meet.example.com \
  --conference-domain conference.meet.example.com \
  --room my-room \
  --duration 20 \
  --output ~/rec.wav
```

If your server requires auth, pass JWT:

```bash
/opt/homebrew/bin/python3.9 bot.py \
  --host meet.example.com \
  --room my-room \
  --token "<jwt>" \
  --output ~/rec.wav
```

## Arguments

- `--room` (required): room name
- `--output`: output WAV path (default `recording.wav`)
- `--host`, `--server`: Jitsi host/domain (default `meet.jit.si`)
- `--xmpp-domain`: optional XMPP auth domain override
- `--conference-domain`: optional MUC domain override
- `--duration`: recording duration in seconds (default: until Ctrl+C)
- `--token`: JWT token for authenticated servers
- `-v`, `--verbose`: verbose logs

## ICE Behavior

By default the bot uses automatic ICE fallback:

- `host` -> `stun` -> `full`

On failure before any audio is captured, it switches to the next mode and logs:

`ICE failed in mode=<current>; switching to fallback mode=<next>`

You can force a start mode for debugging via env var:

```bash
JITSI_BOT_ICE_MODE=host /opt/homebrew/bin/python3.9 bot.py --host meet.jit.si --room test --output ~/rec.wav
```

