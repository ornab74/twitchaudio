# TwitchAudio

A low-bandwidth Twitch audio player with a dark CustomTkinter command-deck UI.

TwitchAudio asks Streamlink for Twitch's `audio_only` stream and pipes it into `ffplay`, so it avoids downloading the 1080p video stream. It also keeps a password-protected history of streams you launch.

## Features

- Audio-only Twitch playback for low-bandwidth connections.
- CustomTkinter desktop UI with stream controls, diagnostics, saved-stream cards, and volume control.
- Password dialog on launch for the encrypted history vault.
- Saved stream history stored in SQLite at `~/.twitchaudio/history.sqlite3`.
- Saved stream payloads are encrypted before they are written to SQLite using a password-derived key.
- One-click BeardHero preset plus reusable history cards.

## Requirements

- Python 3.10 or newer.
- `ffplay`, which ships with FFmpeg.
- Python packages from `requirements.txt`.

On Debian/Ubuntu/ChromeOS Linux:

```bash
sudo apt install ffmpeg python3-tk
python3 -m pip install -r requirements.txt
```

If `streamlink` is not on your `PATH` after installing requirements, try:

```bash
python3 -m pip install --user -r requirements.txt
```

## Run

```bash
python3 main.py
```

On first launch, create a vault password. On later launches, enter the same password to unlock saved streams. If the password is lost, the saved stream history cannot be recovered.

## Bandwidth Notes

The default quality is `audio_only`. Typical Twitch audio-only streams are roughly 60-80 MB per hour, while 1080p video can use multiple GB per hour. Actual usage depends on Twitch's current stream variants and your network.

Changing the volume while audio is already playing restarts the audio pipe after a short debounce so `ffplay` receives the new volume filter. You may hear a brief blip, but the stream stays audio-only.

## Encryption Notes

The app uses SQLite for storage and encrypts each saved stream payload before it enters the database. Basic database structure and timestamps remain visible, but saved stream URLs/titles/settings are encrypted with a key derived from your password.

This is lightweight app-level encryption, not SQLCipher full-file encryption.
