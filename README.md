# TwitchAudio

TwitchAudio is a single-file CustomTkinter desktop app for listening to Twitch streams with a small local footprint. It launches Twitch playback through Streamlink and ffplay, keeps a saved stream history in a local SQLite vault, and can optionally connect to Twitch chat with OAuth credentials stored in that same encrypted vault.

The current app implementation lives in `main.py`.

![TwitchAudio GUI preview](demo.png)

## Current Features

- Audio-only Twitch playback through Streamlink's `audio_only` stream.
- Optional video playback qualities: `160p`, `360p`, `480p`, `720p`, `720p60`, `1080p`, `1080p60`, and `best`.
- Dark CustomTkinter interface with stream controls, saved-stream cards, status text, diagnostics, settings, and chat controls.
- One-click BeardHero preset.
- Saved stream history with play counts, last-played timestamps, playback mode, quality, and volume.
- Password-gated local SQLite vault at first launch.
- AES-256-GCM encrypted JSON payloads with keys derived from the vault password using Scrypt.
- Encrypted storage for Twitch app credentials, OAuth access tokens, OAuth refresh tokens, and chat transcripts.
- Twitch OAuth device-code flow for chat authentication.
- In-app Twitch IRC chat over TLS, with browser popout chat as a no-auth fallback.
- Automatic Twitch access-token refresh before chat/API use.
- Chat message sanitization, bounded chat history, and encrypted chat transcript storage.
- Encrypted password rotation from inside the app.
- Live volume slider; when needed, playback restarts so the new ffplay volume filter takes effect.
- Stream history trimming to the newest 80 records and chat trimming to the newest 400 messages per channel.

## Requirements

### System packages

- Python 3.10 or newer.
- FFmpeg with `ffplay` on your `PATH`.
- Tk support for Python.
- Streamlink, installed through Python dependencies.

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg python3-tk python3-venv
```

Optional Linux audio diagnostics:

```bash
sudo apt install -y alsa-utils
```

### Python packages

`requirements.txt` currently pins the required Python packages:

```txt
customtkinter==5.2.2
cryptography==42.0.0
streamlink==6.8.0
```

`bleach` is used for stronger text sanitization when it is installed, but the app falls back to built-in HTML stripping when it is not available.

## Install From A Local Checkout

```bash
git clone https://github.com/ornab74/twitchaudio.git
cd twitchaudio
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Run

Run the current app directly:

```bash
python3 main.py
```

On Windows, use:

```powershell
python main.py
```

On first launch, create a vault password. On later launches, enter that password to unlock saved streams, settings, OAuth tokens, and chat transcripts.

If the vault password is lost, encrypted saved data cannot be recovered.

## How To Use

1. Start the app with `python3 main.py`.
2. Create or enter your local vault password.
3. Paste a Twitch channel URL, or use the BeardHero preset.
4. Choose `Audio only` for lowest bandwidth, or choose `Video` and select a Streamlink quality.
5. Start playback.
6. Adjust volume if needed.
7. Use saved stream cards to replay, load, or delete previous streams.
8. Open Settings to configure Twitch chat authentication.
9. Use in-app chat after OAuth setup, or open browser popout chat without authentication.
10. Stop playback when finished.

## Twitch Chat Setup

TwitchAudio supports two chat modes:

| Mode | Setup | Notes |
| --- | --- | --- |
| In-app chat | Save a Twitch Client ID and Client Secret in Settings, then generate a device-code token. | Uses Twitch IRC over TLS and can read/send normal chat messages. |
| Browser popout | Click the popout option in the chat area. | Opens Twitch chat in your default browser and does not require storing OAuth credentials. |

For in-app chat, create a Twitch application, copy the Client ID and Client Secret into Settings, then generate a token. TwitchAudio requests these Twitch chat scopes:

```txt
chat:read chat:edit
```

The app stores Twitch credentials and tokens encrypted in the local vault. It refreshes access tokens automatically when they are near expiry.

## Local Storage

TwitchAudio stores data in a SQLite file named `history.sqlite3`.

Default locations:

| OS | Location |
| --- | --- |
| Linux | `$XDG_DATA_HOME/twitchaudio/history.sqlite3` or `~/.local/share/twitchaudio/history.sqlite3` |
| macOS | `~/Library/Application Support/TwitchAudio/history.sqlite3` |
| Windows | `%APPDATA%\\TwitchAudio\\history.sqlite3` |

If an older `~/.twitchaudio` directory already exists, TwitchAudio keeps using it so existing local data remains available.

The SQLite database contains these logical areas:

| Table | Purpose |
| --- | --- |
| `meta` | Vault salt, KDF metadata, verifier, and creation metadata. |
| `streams` | Encrypted saved stream payloads plus visible timestamps and play counts. |
| `settings` | Encrypted Twitch credentials and OAuth token state. |
| `chat_messages` | Encrypted per-channel chat transcript payloads. |

## Security Model

TwitchAudio uses application-level encryption for sensitive payloads. It is designed to keep stream details, chat content, and Twitch credentials private at rest without requiring SQLCipher.

Encrypted:

- Stream title, URL, playback mode, quality, and volume.
- Twitch Client ID and Client Secret.
- Twitch login name.
- Twitch OAuth access token and refresh token.
- Chat message user, body, and direction.

Visible in SQLite:

- Table names and schema.
- Row IDs.
- Stream timestamps and play counts.
- Chat channel names and message timestamps.
- Metadata keys such as `salt`, `kdf`, `verifier`, and `created_at`.

Implementation details:

- Passwords are not stored directly.
- Each vault has a random salt.
- The vault key is derived with Scrypt.
- Payloads are sealed with AES-GCM.
- Password rotation re-encrypts saved stream records, settings, and chat payloads with a new derived key.

For full database-file encryption, use SQLCipher or a platform-level encrypted filesystem. TwitchAudio intentionally keeps setup simple by encrypting sensitive payloads inside the app.

## Playback Notes

Audio-only mode asks Streamlink for the `audio_only` variant and pipes it into `ffplay`.

Video mode lets Streamlink play the selected video quality through `ffplay`. The current allowed quality choices are:

```txt
160p 360p 480p 720p 720p60 1080p 1080p60 best
```

Audio-only mode is the lowest-bandwidth option. Video bandwidth depends on Twitch's available stream variants and the quality selected.

Changing volume may briefly interrupt playback because ffplay receives volume as a startup audio filter. TwitchAudio restarts playback when needed so the new volume takes effect.

## Project Layout

```txt
.
├── main.py
├── requirements.txt
├── pyproject.toml
├── README.md
├── LICENSE
├── .gitignore
└── demo.png
```

Important files:

| File | Purpose |
| --- | --- |
| `main.py` | Current CustomTkinter app, encrypted storage, Twitch OAuth/chat, and Streamlink/ffplay process handling. |
| `requirements.txt` | Pinned runtime Python dependencies for local runs. |
| `pyproject.toml` | Project metadata and dependency declarations. |
| `demo.png` | GUI preview image used by this README. |

## Development Checks

Syntax check:

```bash
python3 -m py_compile main.py
```

Whitespace check:

```bash
git diff --check
```

Check current repo changes:

```bash
git status --short
```

## Troubleshooting

If the app says `Missing Python dependency: customtkinter`, install the Python dependencies in your virtual environment:

```bash
python3 -m pip install -r requirements.txt
```

If the app says `Missing tools: streamlink` or `Missing tools: ffplay`, reinstall dependencies and make sure both tools are on your `PATH`:

```bash
python3 -m pip install -r requirements.txt
sudo apt install -y ffmpeg
```

If the GUI fails to open on Linux, install Tk support:

```bash
sudo apt install -y python3-tk
```

If a Twitch stream will not start, make sure the channel is live and Streamlink can see the selected quality:

```bash
streamlink https://www.twitch.tv/beardhero audio_only --stream-url
```

If Linux audio seems broken outside the app, test the system audio stack:

```bash
speaker-test -t wav -c 2
```

## License

MIT. See `LICENSE`.
