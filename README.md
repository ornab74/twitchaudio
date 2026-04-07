# TwitchAudio

Low-bandwidth Twitch listening with a dark CustomTkinter command deck.

TwitchAudio is a tiny desktop app that asks Streamlink for Twitch's `audio_only` stream and pipes that stream directly into `ffplay`. The goal is simple: hear the stream without downloading the 1080p video feed. It also keeps your saved stream history and Twitch chat settings in a local SQLite vault where sensitive payloads are encrypted with a password-derived key.

![TwitchAudio GUI preview](demo.png)

## Table Of Contents

- [Highlights](#highlights)
- [Design Goals](#design-goals)
- [Architecture](#architecture)
- [Playback Flow](#playback-flow)
- [Vault Unlock Flow](#vault-unlock-flow)
- [Storage Model](#storage-model)
- [Volume Flow](#volume-flow)
- [Requirements](#requirements)
- [Ubuntu/Debian Packages](#ubuntudebian-packages)
- [Install](#install)
- [Run](#run)
- [Package Layout](#package-layout)
- [Publishing To PyPI](#publishing-to-pypi)
- [How To Use](#how-to-use)
- [Twitch Chat](#twitch-chat)
- [Security Model](#security-model)
- [Troubleshooting](#troubleshooting)
- [Project Map](#project-map)
- [Development Checks](#development-checks)

## Highlights

- Audio-only Twitch playback designed for low-bandwidth connections.
- Dark CustomTkinter UI with stream controls, diagnostics, status cards, and saved-stream cards.
- Password gate on launch for the encrypted local history vault.
- Saved stream history stored in the user's platform app-data directory as `history.sqlite3`.
- Stream URLs, display titles, quality, and volume settings encrypted before SQLite writes.
- One-click BeardHero preset.
- Play, load, and delete saved streams from the history panel.
- Optional Twitch chat panel with authenticated in-app IRC chat or browser popout chat.
- Live volume slider with a debounced audio-pipe restart so `ffplay` receives the new filter.
- Change the history vault password from inside the app.
- Clear saved stream history without touching source files.

## Design Goals

- Keep bandwidth tiny by requesting only Twitch's `audio_only` stream variant.
- Avoid saving downloaded audio or video to disk.
- Keep the UI friendly enough for quick use and detailed enough for troubleshooting.
- Encrypt the saved stream payloads without requiring SQLCipher or system database setup.
- Keep the app installable as a Python package with a `twitchaudio` launcher command.

## Architecture

```mermaid
flowchart LR
    User["User"] --> GUI["CustomTkinter GUI"]
    GUI --> PasswordDialog["Password Dialog"]
    PasswordDialog --> HistoryStore["EncryptedHistoryStore"]
    HistoryStore --> KDF["PBKDF2-HMAC-SHA256<br/>600k iterations"]
    KDF --> Fernet["Fernet Key"]
    HistoryStore --> SQLite["SQLite Vault<br/>platform app-data/history.sqlite3"]
    GUI --> StreamControls["URL, audio_only quality, volume"]
    StreamControls --> Streamlink["streamlink --stdout<br/>audio_only"]
    Streamlink --> Pipe["stdout pipe<br/>MPEG-TS audio"]
    Pipe --> FFplay["ffplay -nodisp<br/>volume filter"]
    FFplay --> Speakers["System audio"]
    GUI --> Diagnostics["Diagnostics panel"]
    GUI --> ChatPanel["Chat panel"]
    ChatPanel --> TwitchIRC["Twitch IRC<br/>optional auth"]
    ChatPanel --> BrowserChat["Browser chat popout"]
    GUI --> HistoryCards["Saved stream cards"]
    HistoryCards --> HistoryStore
```

## Playback Flow

```mermaid
sequenceDiagram
    actor User
    participant App as TwitchAudioApp
    participant Store as EncryptedHistoryStore
    participant Streamlink
    participant FFplay
    participant DB as SQLite Vault

    User->>App: Enter Twitch URL and click Start Audio
    App->>App: Validate URL and required tools
    App->>Streamlink: Start streamlink --stdout audio_only
    Streamlink-->>App: Audio bytes on stdout
    App->>FFplay: Start ffplay -nodisp with stdin pipe
    App->>Store: save_launch(url, quality, volume)
    Store->>Store: Encrypt stream payload with Fernet
    Store->>DB: Write encrypted payload and timestamps
    App-->>User: Show Streaming status and diagnostics
    FFplay-->>User: Play audio
```

## Vault Unlock Flow

```mermaid
flowchart TD
    Start["App starts"] --> Gate["Open password dialog"]
    Gate --> FirstRun{"history.sqlite3 exists?"}
    FirstRun -- "No" --> CreateSalt["Generate random salt"]
    CreateSalt --> DeriveNew["Derive Fernet key from password"]
    DeriveNew --> SaveVerifier["Encrypt verifier token"]
    SaveVerifier --> NewVault["Create SQLite vault metadata"]
    NewVault --> Launch["Launch main GUI"]
    FirstRun -- "Yes" --> LoadSalt["Load salt and encrypted verifier"]
    LoadSalt --> DeriveExisting["Derive Fernet key from entered password"]
    DeriveExisting --> Verify{"Verifier decrypts?"}
    Verify -- "Yes" --> Launch
    Verify -- "No" --> Retry["Show unlock error and retry"]
    Retry --> Gate
```

## Storage Model

```mermaid
erDiagram
    META {
        TEXT key PK
        TEXT value
    }

    STREAMS {
        INTEGER id PK
        BLOB payload "Fernet encrypted JSON"
        TEXT created_at
        TEXT updated_at
        TEXT last_played_at
        INTEGER play_count
    }

    SETTINGS {
        TEXT key PK
        BLOB payload "Fernet encrypted JSON"
        TEXT updated_at
    }
```

The `streams.payload` blob contains the encrypted JSON for URL, title, quality, and volume. The `settings.payload` blob contains encrypted app settings such as Twitch chat username and OAuth token. Timestamp and play-count fields stay outside encrypted payloads so the app can sort history without decrypting every metadata field into separate columns.

## Volume Flow

```mermaid
stateDiagram-v2
    [*] --> Ready
    Ready --> Streaming: Start Audio
    Streaming --> Queued: Move volume slider
    Queued --> Queued: More slider movement resets debounce
    Queued --> Restarting: 800ms debounce expires
    Restarting --> Streaming: Restart streamlink and ffplay with new volume
    Streaming --> Ready: Stop
    Restarting --> Ready: Restart fails or Stop
```

`ffplay` receives volume as a startup audio filter, so the app cannot push a new filter into an already-running `ffplay` process. TwitchAudio solves that by waiting briefly after slider movement and restarting the audio pipe with the new volume. You may hear a small blip, but the stream remains `audio_only`.

## Requirements

- Python 3.10 or newer.
- FFmpeg with `ffplay`.
- Tk support for Python.
- Audio test tools from `alsa-utils`.
- Python virtual environment support from `python3-venv`.
- Python packages from `pyproject.toml` or `requirements.txt`.

`requirements.txt` currently pins:

```txt
customtkinter==5.2.2
cryptography==42.0.0
streamlink==6.8.0
```

The package metadata in `pyproject.toml` uses compatible minimum versions for distribution:

```txt
customtkinter>=5.2.2
cryptography>=42.0.0
streamlink>=6.8.0
```

## Ubuntu/Debian Packages

Install the system packages first:

```bash
sudo apt update
sudo apt install -y ffmpeg python3-tk python3-venv alsa-utils
```

What those packages provide:

| Package | Why it is needed |
| --- | --- |
| `ffmpeg` | Provides `ffplay`, which plays Twitch `audio_only` MPEG-TS/AAC audio reliably. |
| `python3-tk` | Provides Tkinter support required by CustomTkinter. |
| `python3-venv` | Provides `python3 -m venv` for isolated Python installs. |
| `alsa-utils` | Provides audio tools like `speaker-test` and `aplay` for Linux audio diagnostics. |

Optional diagnostic player:

```bash
sudo apt install -y mpg123
```

`mpg123` is not required by the app. It was useful during troubleshooting, but the final app uses `ffplay` because Twitch's `audio_only` stream is typically AAC audio inside an MPEG-TS/HLS stream, not plain MP3.

## Install

From a local checkout on Ubuntu or Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg python3-tk python3-venv alsa-utils
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
```

For editable development installs:

```bash
python3 -m pip install -e .
```

After this project is published to PyPI, users should be able to install it with:

```bash
python3 -m pip install twitchaudio
```

Important: `pip install twitchaudio` installs the Python package and the `twitchaudio` command, but users still need `ffplay` from FFmpeg installed on their operating system.

FFmpeg install examples:

| OS | Command |
| --- | --- |
| Ubuntu/Debian | `sudo apt install -y ffmpeg` |
| macOS with Homebrew | `brew install ffmpeg` |
| Windows with winget | `winget install Gyan.FFmpeg` |

## Run

After package install:

```bash
twitchaudio
```

Alternative module form:

```bash
python3 -m twitchaudio
```

Local development shim:

```bash
python3 main.py
```

On first launch, create a vault password. On later launches, enter the same password to unlock saved streams.

If the password is lost, the saved stream history cannot be recovered.

## Package Layout

```txt
.
├── pyproject.toml
├── main.py
├── src/
│   └── twitchaudio/
│       ├── __init__.py
│       ├── __main__.py
│       └── app.py
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
└── demo.png
```

Package entry points:

| Command | Entry point | Notes |
| --- | --- | --- |
| `twitchaudio` | `twitchaudio.app:main` | Normal CLI launcher that opens the GUI. |
| `twitchaudio-gui` | `twitchaudio.app:main` | GUI launcher style, useful for desktop shortcuts on platforms that support GUI scripts. |

`main.py` is only a local development shim. The installed package runs from `src/twitchaudio/app.py`.

## Publishing To PyPI

Before publishing, confirm the name `twitchaudio` is available on PyPI. If it is taken, change `[project].name` in `pyproject.toml`; the installed command can still stay `twitchaudio`.

Build the package:

```bash
python3 -m pip install --upgrade build twine
python3 -m build
```

Check the distribution:

```bash
python3 -m twine check dist/*
```

Upload to TestPyPI first:

```bash
python3 -m twine upload --repository testpypi dist/*
```

Upload to PyPI when ready:

```bash
python3 -m twine upload dist/*
```

Install test:

```bash
python3 -m pip install twitchaudio
twitchaudio
```

## How To Use

1. Launch the app with `twitchaudio`.
2. Enter or create your history vault password.
3. Paste a Twitch stream URL, or click `Use BeardHero`.
4. Keep quality on `audio_only`.
5. Click `Start Audio`.
6. Move the volume slider if needed.
7. Use `Play`, `Load`, or `Delete` on saved history cards.
8. Use `Connect` in the Twitch Chat panel if you configured chat auth, or `Open Popout` for browser chat.
9. Click `Stop` when you are done.

## Twitch Chat

TwitchAudio supports chat in two ways:

| Mode | Setup | Notes |
| --- | --- | --- |
| In-app chat | Open `Settings` and save your Twitch username plus OAuth token. | Chat appears inside the GUI and can send messages. |
| Browser popout | Click `Open Popout`. | Opens Twitch chat in your default browser and does not need app chat auth. |

For in-app chat, create a Twitch user OAuth token with these scopes:

```txt
chat:read chat:write
```

Then open `Settings`, paste your Twitch username and token, and click `Save`. The token may include or omit the `oauth:` prefix. TwitchAudio adds it if needed.

Twitch chat settings are encrypted in the same local vault used for stream history. They are not stored in environment variables.

The in-app chat client supports normal chat messages. It does not send moderation commands.

## How It Saves Streams

When a stream starts, TwitchAudio creates a payload with the stream title, URL, quality, and volume. That payload is serialized to JSON, encrypted with Fernet, and stored in SQLite. The saved-stream card view decrypts records only after the vault is unlocked with the correct password.

The app trims history to the newest 80 saved records.

Vault location:

| OS | Default location |
| --- | --- |
| Linux | `$XDG_DATA_HOME/twitchaudio/history.sqlite3` or `~/.local/share/twitchaudio/history.sqlite3` |
| macOS | `~/Library/Application Support/TwitchAudio/history.sqlite3` |
| Windows | `%APPDATA%\\TwitchAudio\\history.sqlite3` |

If an older `~/.twitchaudio` directory already exists, TwitchAudio keeps using it so existing local history stays available.

## Bandwidth Notes

TwitchAudio is locked to `audio_only`, so it does not accidentally request video variants.

Typical Twitch audio-only usage is roughly 60-80 MB per hour. A 1080p stream can use multiple GB per hour. Actual bandwidth depends on Twitch's current stream variants and your network.

## Security Model

TwitchAudio uses lightweight app-level encryption. It is useful for keeping saved stream details private at rest, but it is not the same thing as full database encryption.

Encrypted:

- Stream URL.
- Display title.
- Quality setting.
- Volume setting.
- Twitch chat username.
- Twitch chat OAuth token.

Still visible in SQLite:

- Table structure.
- Row IDs.
- Created, updated, and last-played timestamps.
- Play counts.
- Metadata keys such as `salt`, `verifier`, and `created_at`.

Key details:

- Passwords are not stored directly.
- A random salt is generated for the vault.
- Keys are derived with PBKDF2-HMAC-SHA256 using 600,000 iterations.
- A short verifier token is encrypted to confirm whether the password can unlock the vault.
- Changing the vault password re-encrypts saved stream payloads with a new password-derived key.

If you need full-file encrypted SQLite, use SQLCipher or a platform-level encrypted filesystem. This project intentionally keeps setup simple by encrypting payloads in the app.

## Troubleshooting

If the app says `Missing Python dependency: customtkinter`, install Python dependencies:

```bash
python3 -m pip install .
```

If the app says `Missing tools: streamlink` or `Missing tools: ffplay`, install dependencies and make sure they are on your `PATH`:

```bash
sudo apt update
sudo apt install -y ffmpeg python3-tk python3-venv alsa-utils
python3 -m pip install .
```

If audio does not start, make sure the Twitch channel is live and Streamlink can see an `audio_only` variant:

```bash
streamlink https://www.twitch.tv/beardhero audio_only --stream-url
```

If the GUI fails to open on Linux, make sure Tk is installed:

```bash
sudo apt install -y python3-tk
```

If you want to test whether Linux audio works before launching the app:

```bash
speaker-test -t wav -c 2
```

If volume changes cause a brief cutout, that is expected. TwitchAudio restarts `ffplay` so the new volume filter takes effect.

## Project Map

```txt
.
├── pyproject.toml
├── main.py
├── src/
│   └── twitchaudio/
│       ├── __init__.py
│       ├── __main__.py
│       └── app.py
├── requirements.txt
├── README.md
├── LICENSE
├── .gitignore
└── demo.png
```

## Code Map

- `EncryptedHistoryStore`: SQLite schema, vault unlock, encryption, decryption, password rotation, history trimming.
- `PasswordDialog`: first-run vault creation and later unlock dialog.
- `PasswordChangeDialog`: vault password rotation UI.
- `SettingsDialog`: encrypted Twitch chat username and OAuth token settings UI.
- `StreamCard`: saved-stream card UI.
- `TwitchChatReader`: Twitch IRC reader and sender used by the in-app chat panel.
- `TwitchAudioApp`: main window, Streamlink/ffplay process lifecycle, diagnostics, live volume debounce, history actions, settings, and chat actions.
- `unlock_history`: password-gated app startup.
- `src/twitchaudio/app.py`: CustomTkinter theme setup and app launch.
- `main.py`: local development shim.
- `pyproject.toml`: package metadata and `twitchaudio` entry point.

## Process Commands

The app starts Streamlink like this:

```bash
streamlink --loglevel none --stdout --twitch-disable-ads --stream-segment-threads 2 https://www.twitch.tv/beardhero audio_only
```

The Streamlink stdout pipe is connected into ffplay like this:

```bash
ffplay -nodisp -autoexit -f mpegts -af volume=2.0 -fflags nobuffer -flags low_delay -
```

The `-` at the end tells `ffplay` to read from standard input.

## Development Checks

Syntax check:

```bash
python3 -m py_compile main.py src/twitchaudio/app.py
```

Whitespace check:

```bash
git diff --check
```

Check current repo changes:

```bash
git status --short
```

Build check:

```bash
python3 -m build
```

## License

MIT. See `LICENSE`.
