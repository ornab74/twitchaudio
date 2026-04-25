import base64
import binascii
import ctypes
import ctypes.util
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import socket
import ssl
import webbrowser
import html
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    from tkinter import BooleanVar, PhotoImage, messagebox

    import customtkinter as ctk

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
except ModuleNotFoundError as exc:
    missing = exc.name or "a required package"
    print(f"Missing Python dependency: {missing}")
    if missing == "tkinter":
        print("On Ubuntu/Debian, install Tk support with: sudo apt install python3-tk")
    else:
        print("Install Python dependencies with: python3 -m pip install .")
    sys.exit(1)

try:
    import bleach
except ModuleNotFoundError:
    bleach = None


APP_NAME = "TwitchAudio"
DEFAULT_STREAM_URL = "https://www.twitch.tv/beardhero"
LOGO_PATH = Path(__file__).resolve().parent / "logo.png"
LEGACY_APP_DIR = Path.home() / ".twitchaudio"


def get_app_dir() -> Path:
    if LEGACY_APP_DIR.exists():
        return LEGACY_APP_DIR

    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "twitchaudio"

    return Path.home() / ".local" / "share" / "twitchaudio"


APP_DIR = get_app_dir()
DB_PATH = APP_DIR / "history.sqlite3"
SALT_BYTES = 16
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
VERIFY_TEXT = b"twitchaudio-history-v1"
AES_ENVELOPE_MAGIC = b"TAG1"
MAX_HISTORY = 80
TWITCH_IRC_HOST = "irc.chat.twitch.tv"
TWITCH_IRC_PORT = 6697
CHAT_CREDENTIALS_KEY = "twitch_chat_credentials"
TWITCH_OAUTH_KEY = "twitch_oauth"
TWITCH_DEVICE_ENDPOINT = "https://id.twitch.tv/oauth2/device"
TWITCH_TOKEN_ENDPOINT = "https://id.twitch.tv/oauth2/token"
TWITCH_VALIDATE_ENDPOINT = "https://id.twitch.tv/oauth2/validate"
TWITCH_HELIX_ENDPOINT = "https://api.twitch.tv/helix"
TWITCH_CHAT_SCOPES = "chat:read chat:edit"
PLAYBACK_AUDIO_ONLY = "Audio only"
PLAYBACK_LOW_VIDEO = "Video"
QUALITY_AUDIO_ONLY = "audio_only"
LOW_VIDEO_QUALITIES = ("160p", "360p", "480p", "720p", "720p60", "1080p", "1080p60", "best")
PLAYBACK_QUALITIES = (QUALITY_AUDIO_ONLY, *LOW_VIDEO_QUALITIES)
MAX_CHAT_LINES = 400
MAX_CHAT_MESSAGE_CHARS = 500
MAX_CHAT_USER_CHARS = 32
CHAT_TRIM_INTERVAL = 25
CHAT_UI_TRIM_BATCH = 50
CHAT_SAVE_BATCH_MS = 1000
VIDEO_RETRY_LIMIT = 60
VIDEO_RETRY_BASE_MS = 800
VIDEO_RETRY_MAX_MS = 12000
VIDEO_CACHE_SECONDS = 90
STREAMLINK_RINGBUFFER_SIZE = "256M"
STREAMLINK_SEGMENT_THREADS = "4"
CHAT_SOCKET_TIMEOUT_SECONDS = 2.0
ONLINE_STATUS_REFRESH_SECONDS = 300
EXPLORE_CACHE_SECONDS = 120
EVENT_POLL_IDLE_MS = 2000
EVENT_POLL_ACTIVE_MS = 250
MAX_EVENTS_PER_TICK = 80
PROCESS_MONITOR_INTERVAL_SECONDS = 3.0
PROCESS_HEARTBEAT_SECONDS = 30.0
DIAGNOSTIC_LOG_INTERVAL_SECONDS = 1.5
VIDEO_READY_MESSAGE = "Video is ready. Choose Video mode and a resolution. Use Player Window for lower CPU, or double-click the in-app video for fullscreen."
TWITCH_CATEGORY_IDS = {
    "Software and Game Development": "1469308723",
    "Science & Technology": "509670",
    "Science and Technology": "509670",
    "Just Chatting": "509658",
    "Music": "26936",
    "Art": "509660",
    "Makers & Crafting": "509673",
    "Food & Drink": "509667",
    "Sports": "518203",
    "Talk Shows & Podcasts": "417752",
    "Special Events": "509663",
}


def sanitize_text(value: Any, max_chars: int = 500) -> str:
    text = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
    text = text[:max_chars]
    if bleach is not None:
        text = bleach.clean(text, tags=[], attributes={}, protocols=[], strip=True)
    else:
        text = re.sub(r"<[^>]*>", "", html.unescape(text))
    return text.strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_time(value: str | None) -> str:
    if not value:
        return "Never"

    try:
        parsed = datetime.fromisoformat(value)
        local = parsed.astimezone()
        return local.strftime("%b %d, %Y %I:%M %p")
    except ValueError:
        return value


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(
        salt=salt,
        length=32,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
    )
    return kdf.derive(password.encode("utf-8"))


def channel_name_from_url(url: str) -> str:
    parsed = urlparse(sanitize_text(url, max_chars=300))
    if parsed.netloc and "twitch.tv" in parsed.netloc.lower():
        channel = parsed.path.strip("/").split("/")[0]
        if channel:
            return channel
    return parsed.netloc or url.strip()


def twitch_channel_from_url(url: str) -> str | None:
    candidate = channel_name_from_url(url).strip().lstrip("#").lower()
    if re.fullmatch(r"[a-z0-9_]{3,25}", candidate):
        return candidate
    return None


def looks_like_url(url: str) -> bool:
    parsed = urlparse(sanitize_text(url, max_chars=300))
    return parsed.scheme == "https" and parsed.netloc.lower() in {"www.twitch.tv", "twitch.tv"}


def irc_unescape(value: str) -> str:
    return (
        value.replace(r"\s", " ")
        .replace(r"\:", ";")
        .replace(r"\r", "\r")
        .replace(r"\n", "\n")
        .replace(r"\\", "\\")
    )


def parse_irc_tags(line: str) -> tuple[dict[str, str], str]:
    if not line.startswith("@"):
        return {}, line

    raw_tags, _, remaining = line.partition(" ")
    tags: dict[str, str] = {}
    for tag in raw_tags[1:].split(";"):
        key, _, value = tag.partition("=")
        tags[key] = irc_unescape(value)
    return tags, remaining


def parse_privmsg(line: str) -> tuple[str, str] | None:
    tags, message_line = parse_irc_tags(line)
    match = re.match(r":([^!]+)![^ ]+ PRIVMSG #[^ ]+ :(.+)", message_line)
    if not match:
        return None

    display_name = tags.get("display-name") or match.group(1)
    message = match.group(2)
    return display_name, message


def format_chat_token(token: str) -> str:
    token = sanitize_text(token, max_chars=256)
    if token.startswith("oauth:"):
        return token
    return f"oauth:{token}"


def sanitize_chat_user(value: Any) -> str:
    user = sanitize_text(value, max_chars=MAX_CHAT_USER_CHARS)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", user):
        return "unknown"
    return user


def sanitize_chat_message(value: Any) -> str:
    return sanitize_text(value, max_chars=MAX_CHAT_MESSAGE_CHARS)


@dataclass
class StreamRecord:
    id: int
    title: str
    url: str
    playback_mode: str
    quality: str
    volume: float
    created_at: str
    updated_at: str
    last_played_at: str | None
    play_count: int


@dataclass
class ChatRecord:
    id: int
    channel: str
    user: str
    message: str
    direction: str
    created_at: str


@dataclass
class TwitchDeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class EncryptedHistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.aes_key: bytes | None = None
        self.chat_trim_counts: dict[str, int] = {}

    @property
    def is_new(self) -> bool:
        return not self.path.exists()

    def unlock(self, password: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._ensure_schema()

        salt_value = self._get_meta("salt")
        if not salt_value:
            salt = os.urandom(SALT_BYTES)
            key = derive_key(password, salt)
            self.aes_key = key
            self._set_meta("salt", base64.urlsafe_b64encode(salt).decode("ascii"))
            self._set_meta("kdf", f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}")
            self._set_meta("verifier", base64.b64encode(self._encrypt_bytes(VERIFY_TEXT, b"meta:verifier")).decode("ascii"))
            self._set_meta("created_at", utc_now())
            self.connection.commit()
            return

        try:
            salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
            key = derive_key(password, salt)
            self.aes_key = key
            verifier = self._get_meta("verifier") or ""
            verifier_blob = base64.b64decode(verifier.encode("ascii"))
            if self._decrypt_bytes(verifier_blob, b"meta:verifier") != VERIFY_TEXT:
                raise ValueError
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            self.close()
            raise ValueError("That password could not unlock this history vault.") from exc

    def close(self) -> None:
        if self.connection:
            self.connection.close()
        self.connection = None
        self.aes_key = None

    def _ensure_schema(self) -> None:
        assert self.connection is not None
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_played_at TEXT,
                play_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_channel_created ON chat_messages(channel, created_at DESC)"
        )
        self.connection.commit()

    def _get_meta(self, key: str) -> str | None:
        assert self.connection is not None
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        assert self.connection is not None
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _encrypt_payload(self, payload: dict[str, Any]) -> bytes:
        if not self.aes_key:
            raise RuntimeError("History vault is locked.")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._encrypt_bytes(raw, b"payload:v1")

    def _encrypt_bytes(self, raw: bytes, aad: bytes) -> bytes:
        if not self.aes_key:
            raise RuntimeError("History vault is locked.")
        nonce = os.urandom(12)
        return AES_ENVELOPE_MAGIC + nonce + AESGCM(self.aes_key).encrypt(nonce, raw, aad)

    def _decrypt_payload(self, payload: bytes) -> dict[str, Any]:
        raw = self._decrypt_bytes(payload, b"payload:v1")
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Encrypted payload was not a JSON object.")
        return decoded

    def _decrypt_bytes(self, payload: bytes, aad: bytes) -> bytes:
        if not self.aes_key:
            raise RuntimeError("History vault is locked.")
        if not payload.startswith(AES_ENVELOPE_MAGIC) or len(payload) < 33:
            raise ValueError("Encrypted payload is not an AES-GCM envelope.")
        nonce = payload[4:16]
        encrypted = payload[16:]
        try:
            return AESGCM(self.aes_key).decrypt(nonce, encrypted, aad)
        except InvalidTag as exc:
            raise ValueError("Encrypted payload could not be authenticated.") from exc

    def list_streams(self) -> list[StreamRecord]:
        assert self.connection is not None
        records: list[StreamRecord] = []
        rows = self.connection.execute(
            """
            SELECT id, payload, created_at, updated_at, last_played_at, play_count
            FROM streams
            ORDER BY COALESCE(last_played_at, updated_at, created_at) DESC, id DESC
            """
        ).fetchall()

        for row in rows:
            try:
                payload = self._decrypt_payload(row["payload"])
            except (ValueError, json.JSONDecodeError):
                continue

            records.append(
                StreamRecord(
                    id=int(row["id"]),
                    title=str(payload.get("title") or channel_name_from_url(str(payload.get("url", "")))),
                    url=str(payload.get("url") or ""),
                    playback_mode=str(payload.get("playback_mode") or (PLAYBACK_LOW_VIDEO if str(payload.get("quality")) in LOW_VIDEO_QUALITIES else PLAYBACK_AUDIO_ONLY)),
                    quality=str(payload.get("quality") or "audio_only"),
                    volume=float(payload.get("volume") or 2.0),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                    last_played_at=row["last_played_at"],
                    play_count=int(row["play_count"] or 0),
                )
            )

        return records

    def save_launch(
        self,
        url: str,
        quality: str,
        volume: float,
        count_play: bool = True,
        playback_mode: str | None = None,
    ) -> StreamRecord:
        assert self.connection is not None
        normalized_url = url.strip()
        title = channel_name_from_url(normalized_url)
        now = utc_now()

        for record in self.list_streams():
            if record.url == normalized_url:
                payload = {
                    "title": record.title or title,
                    "url": normalized_url,
                    "playback_mode": playback_mode or record.playback_mode,
                    "quality": quality,
                    "volume": volume,
                }
                self.connection.execute(
                    """
                    UPDATE streams
                    SET payload = ?, updated_at = ?, last_played_at = ?, play_count = play_count + ?
                    WHERE id = ?
                    """,
                    (
                        self._encrypt_payload(payload),
                        now,
                        now if count_play else record.last_played_at,
                        1 if count_play else 0,
                        record.id,
                    ),
                )
                self.connection.commit()
                return self.get_stream(record.id)

        payload = {
            "title": title,
            "url": normalized_url,
            "playback_mode": playback_mode or (PLAYBACK_LOW_VIDEO if quality in LOW_VIDEO_QUALITIES else PLAYBACK_AUDIO_ONLY),
            "quality": quality,
            "volume": volume,
        }
        cursor = self.connection.execute(
            """
            INSERT INTO streams (payload, created_at, updated_at, last_played_at, play_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (self._encrypt_payload(payload), now, now, now if count_play else None, 1 if count_play else 0),
        )
        self.connection.commit()
        self._trim_history()
        return self.get_stream(int(cursor.lastrowid))

    def get_stream(self, record_id: int) -> StreamRecord:
        for record in self.list_streams():
            if record.id == record_id:
                return record
        raise KeyError(f"Stream record {record_id} was not found.")

    def delete_stream(self, record_id: int) -> None:
        assert self.connection is not None
        self.connection.execute("DELETE FROM streams WHERE id = ?", (record_id,))
        self.connection.commit()

    def clear_history(self) -> None:
        assert self.connection is not None
        self.connection.execute("DELETE FROM streams")
        self.connection.commit()

    def save_chat_message(self, channel: str, user: str, message: str, direction: str) -> None:
        self.save_chat_messages([(channel, user, message, direction)])

    def save_chat_messages(self, messages: list[tuple[str, str, str, str]]) -> None:
        assert self.connection is not None
        touched_channels: set[str] = set()
        for channel, user, message, direction in messages:
            clean_channel = sanitize_chat_user(channel.lower())
            if clean_channel == "unknown":
                continue
            clean_direction = "out" if direction == "out" else "in"
            payload = {
                "user": sanitize_chat_user(user),
                "message": sanitize_chat_message(message),
                "direction": clean_direction,
            }
            self.connection.execute(
                """
                INSERT INTO chat_messages (channel, payload, created_at)
                VALUES (?, ?, ?)
                """,
                (clean_channel, self._encrypt_payload(payload), utc_now()),
            )
            self.chat_trim_counts[clean_channel] = self.chat_trim_counts.get(clean_channel, 0) + 1
            touched_channels.add(clean_channel)
        for channel in touched_channels:
            if self.chat_trim_counts.get(channel, 0) >= CHAT_TRIM_INTERVAL:
                self.chat_trim_counts[channel] = 0
                self._trim_chat_messages(channel)
        self.connection.commit()

    def list_chat_messages(self, channel: str, limit: int = 80) -> list[ChatRecord]:
        assert self.connection is not None
        clean_channel = sanitize_chat_user(channel.lower())
        if clean_channel == "unknown":
            return []
        rows = self.connection.execute(
            """
            SELECT id, channel, payload, created_at
            FROM chat_messages
            WHERE channel = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (clean_channel, max(1, min(limit, MAX_CHAT_LINES))),
        ).fetchall()
        records: list[ChatRecord] = []
        for row in reversed(rows):
            try:
                payload = self._decrypt_payload(row["payload"])
            except (ValueError, json.JSONDecodeError):
                continue
            records.append(
                ChatRecord(
                    id=int(row["id"]),
                    channel=str(row["channel"]),
                    user=sanitize_chat_user(payload.get("user")),
                    message=sanitize_chat_message(payload.get("message")),
                    direction="out" if payload.get("direction") == "out" else "in",
                    created_at=str(row["created_at"]),
                )
            )
        return records

    def get_secret_setting(self, key: str) -> dict[str, Any] | None:
        assert self.connection is not None
        row = self.connection.execute("SELECT payload FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return self._decrypt_payload(row["payload"])
        except (ValueError, json.JSONDecodeError):
            return None

    def set_secret_setting(self, key: str, payload: dict[str, Any]) -> None:
        assert self.connection is not None
        self.connection.execute(
            """
            INSERT INTO settings (key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (key, self._encrypt_payload(payload), utc_now()),
        )
        self.connection.commit()

    def delete_secret_setting(self, key: str) -> None:
        assert self.connection is not None
        self.connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        self.connection.commit()

    def list_secret_settings(self) -> dict[str, dict[str, Any]]:
        assert self.connection is not None
        settings: dict[str, dict[str, Any]] = {}
        rows = self.connection.execute("SELECT key, payload FROM settings").fetchall()
        for row in rows:
            try:
                settings[str(row["key"])] = self._decrypt_payload(row["payload"])
            except (ValueError, json.JSONDecodeError):
                continue
        return settings

    def change_password(self, new_password: str) -> None:
        assert self.connection is not None
        records = self.list_streams()
        settings = self.list_secret_settings()
        chat_rows = self.connection.execute(
            "SELECT id, payload FROM chat_messages ORDER BY id ASC"
        ).fetchall()
        chat_payloads: list[tuple[int, dict[str, Any]]] = []
        for row in chat_rows:
            try:
                chat_payloads.append((int(row["id"]), self._decrypt_payload(row["payload"])))
            except (ValueError, json.JSONDecodeError):
                continue
        salt = os.urandom(SALT_BYTES)
        self.aes_key = derive_key(new_password, salt)
        self._set_meta("salt", base64.urlsafe_b64encode(salt).decode("ascii"))
        self._set_meta("kdf", f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}")
        self._set_meta("verifier", base64.b64encode(self._encrypt_bytes(VERIFY_TEXT, b"meta:verifier")).decode("ascii"))

        for record in records:
            payload = {
                "title": record.title,
                "url": record.url,
                "playback_mode": record.playback_mode,
                "quality": record.quality,
                "volume": record.volume,
            }
            self.connection.execute(
                "UPDATE streams SET payload = ?, updated_at = ? WHERE id = ?",
                (self._encrypt_payload(payload), utc_now(), record.id),
            )

        for key, payload in settings.items():
            self.connection.execute(
                "UPDATE settings SET payload = ?, updated_at = ? WHERE key = ?",
                (self._encrypt_payload(payload), utc_now(), key),
            )

        for row_id, payload in chat_payloads:
            self.connection.execute(
                "UPDATE chat_messages SET payload = ? WHERE id = ?",
                (self._encrypt_payload(payload), row_id),
            )

        self.connection.commit()

    def _trim_history(self) -> None:
        assert self.connection is not None
        stale_ids = self.connection.execute(
            """
            SELECT id
            FROM streams
            ORDER BY COALESCE(last_played_at, updated_at, created_at) DESC, id DESC
            LIMIT -1 OFFSET ?
            """,
            (MAX_HISTORY,),
        ).fetchall()
        for row in stale_ids:
            self.connection.execute("DELETE FROM streams WHERE id = ?", (row["id"],))
        self.connection.commit()

    def _trim_chat_messages(self, channel: str) -> None:
        assert self.connection is not None
        self.connection.execute(
            """
            DELETE FROM chat_messages
            WHERE channel = ?
              AND id NOT IN (
                  SELECT id
                  FROM chat_messages
                  WHERE channel = ?
                  ORDER BY created_at DESC, id DESC
                  LIMIT ?
              )
            """,
            (channel, channel, MAX_CHAT_LINES),
        )


class TwitchOAuthManager:
    def __init__(self, history: EncryptedHistoryStore) -> None:
        self.history = history

    def get_state(self) -> dict[str, Any]:
        return self.history.get_secret_setting(TWITCH_OAUTH_KEY) or {}

    def save_app_credentials(self, client_id: str, client_secret: str) -> None:
        state = self.get_state()
        state["client_id"] = sanitize_text(client_id, max_chars=128)
        state["client_secret"] = sanitize_text(client_secret, max_chars=256)
        self.history.set_secret_setting(TWITCH_OAUTH_KEY, state)

    def clear(self) -> None:
        self.history.delete_secret_setting(TWITCH_OAUTH_KEY)

    def has_app_credentials(self) -> bool:
        state = self.get_state()
        return bool(state.get("client_id")) and bool(state.get("client_secret"))

    def begin_device_flow(self) -> TwitchDeviceCode:
        state = self.get_state()
        client_id = sanitize_text(state.get("client_id"), max_chars=128)
        if not client_id:
            raise ValueError("Save your Twitch Client ID first.")

        payload = self._post_form(
            TWITCH_DEVICE_ENDPOINT,
            {
                "client_id": client_id,
                "scopes": TWITCH_CHAT_SCOPES,
            },
        )
        return TwitchDeviceCode(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=str(payload["verification_uri"]),
            expires_in=int(payload.get("expires_in") or 600),
            interval=max(1, int(payload.get("interval") or 5)),
        )

    def poll_device_flow(
        self,
        device_code: str,
        interval: int,
        expires_in: int,
        stop_event: threading.Event,
        status_callback: Callable[[str], None],
    ) -> dict[str, Any]:
        state = self.get_state()
        client_id = sanitize_text(state.get("client_id"), max_chars=128)
        client_secret = sanitize_text(state.get("client_secret"), max_chars=256)
        deadline = time.time() + max(60, expires_in)
        wait_seconds = max(1, interval)
        while time.time() < deadline and not stop_event.is_set():
            time.sleep(wait_seconds)
            try:
                token_payload = self._post_form(
                    TWITCH_TOKEN_ENDPOINT,
                    {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                )
            except RuntimeError as exc:
                detail = str(exc)
                if "authorization_pending" in detail:
                    status_callback("Waiting for Twitch approval...")
                    continue
                if "slow_down" in detail:
                    wait_seconds += 2
                    status_callback("Twitch asked us to slow polling.")
                    continue
                if "expired_token" in detail:
                    raise RuntimeError("The Twitch login code expired. Generate a new code.") from exc
                raise

            saved = self._save_token_payload(token_payload)
            status_callback(f"Authorized as {saved.get('login', 'Twitch user')}.")
            return saved

        raise RuntimeError("Twitch login was not completed before the code expired.")

    def get_chat_identity(self) -> tuple[str, str]:
        token = self.get_valid_access_token()
        state = self.get_state()
        login = sanitize_chat_user(state.get("login"))
        if login == "unknown":
            login = self.validate_access_token(token).get("login", "unknown")
        if login == "unknown":
            raise RuntimeError("Twitch token is valid, but Twitch did not return a login name.")
        return login, format_chat_token(token)

    def get_valid_access_token(self) -> str:
        state = self.get_state()
        token = sanitize_text(state.get("access_token"), max_chars=4096)
        expires_at = float(state.get("expires_at") or 0)
        if token and expires_at > time.time() + 90:
            return token
        return self.refresh_access_token()

    def refresh_access_token(self) -> str:
        state = self.get_state()
        client_id = sanitize_text(state.get("client_id"), max_chars=128)
        client_secret = sanitize_text(state.get("client_secret"), max_chars=256)
        refresh_token = sanitize_text(state.get("refresh_token"), max_chars=4096)
        if not client_id or not client_secret or not refresh_token:
            raise RuntimeError("Twitch login is not complete. Generate a chat token in Settings.")
        token_payload = self._post_form(
            TWITCH_TOKEN_ENDPOINT,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        saved = self._save_token_payload(token_payload)
        return str(saved["access_token"])

    def validate_access_token(self, access_token: str) -> dict[str, Any]:
        request = urllib.request.Request(
            TWITCH_VALIDATE_ENDPOINT,
            headers={"Authorization": f"OAuth {access_token}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(64 * 1024)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Twitch returned an invalid validation response.")
        state = self.get_state()
        if payload.get("login"):
            state["login"] = sanitize_chat_user(payload.get("login"))
            self.history.set_secret_setting(TWITCH_OAUTH_KEY, state)
        return payload

    def helix_get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        state = self.get_state()
        client_id = sanitize_text(state.get("client_id"), max_chars=128)
        if not client_id:
            raise RuntimeError("Twitch Client ID is not configured.")
        token = self.get_valid_access_token()
        query = urllib.parse.urlencode(params or [])
        url = f"{TWITCH_HELIX_ENDPOINT}{path}"
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Id": client_id,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(256 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(128 * 1024).decode("utf-8", errors="replace")
            raise RuntimeError(detail or str(exc)) from exc
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Twitch returned an invalid API response.")
        return payload

    def get_online_channels(self, channels: list[str]) -> set[str]:
        clean_channels = sorted({sanitize_chat_user(channel.lower()) for channel in channels})
        clean_channels = [channel for channel in clean_channels if channel != "unknown"]
        online: set[str] = set()
        for index in range(0, len(clean_channels), 100):
            params = [("user_login", channel) for channel in clean_channels[index : index + 100]]
            payload = self.helix_get("/streams", params)
            for item in payload.get("data", []):
                if isinstance(item, dict) and item.get("user_login"):
                    online.add(sanitize_chat_user(str(item["user_login"]).lower()))
        return online

    def get_category_streams(self, category: str, limit: int = 30) -> list[dict[str, str]]:
        category = sanitize_text(category, max_chars=80)
        category_id = ""
        games = self.helix_get("/games", [("name", category)])
        for item in games.get("data", []):
            if not isinstance(item, dict):
                continue
            name = sanitize_text(item.get("name"), max_chars=80)
            if name.lower() == category.lower():
                category_id = str(item.get("id") or "")
                break
        if not category_id:
            category_id = TWITCH_CATEGORY_IDS.get(category, "")
        if not category_id:
            query = "Science & Technology" if category.lower() == "science and technology" else category
            search = self.helix_get("/search/categories", [("query", query), ("first", "10")])
            for item in search.get("data", []):
                if not isinstance(item, dict):
                    continue
                name = sanitize_text(item.get("name"), max_chars=80)
                if name.lower() in {category.lower(), query.lower()}:
                    category_id = str(item.get("id") or "")
                    break
            if not category_id and search.get("data"):
                first = search["data"][0]
                if isinstance(first, dict):
                    category_id = str(first.get("id") or "")
        if not category_id:
            return []

        payload = self.helix_get("/streams", [("game_id", category_id), ("first", str(max(1, min(limit, 100))))])
        streams: list[dict[str, str]] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            login = sanitize_chat_user(item.get("user_login"))
            title = sanitize_chat_message(item.get("title"))
            display_name = sanitize_text(item.get("user_name"), max_chars=80) or login
            if login != "unknown":
                streams.append(
                    {
                        "login": login,
                        "display_name": display_name,
                        "title": title,
                        "url": f"https://www.twitch.tv/{login}",
                    }
                )
        return streams

    def get_top_categories(self, limit: int = 60) -> list[str]:
        payload = self.helix_get("/games/top", [("first", str(max(1, min(limit, 100))))])
        categories: list[str] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            name = sanitize_text(item.get("name"), max_chars=80)
            if name and name not in categories:
                categories.append(name)
        return categories

    def _save_token_payload(self, token_payload: dict[str, Any]) -> dict[str, Any]:
        state = self.get_state()
        access_token = sanitize_text(token_payload.get("access_token"), max_chars=4096)
        refresh_token = sanitize_text(token_payload.get("refresh_token"), max_chars=4096)
        if not access_token or not refresh_token:
            raise RuntimeError("Twitch did not return both access and refresh tokens.")
        state["access_token"] = access_token
        state["refresh_token"] = refresh_token
        state["expires_at"] = time.time() + int(token_payload.get("expires_in") or 0)
        state["scope"] = token_payload.get("scope") if isinstance(token_payload.get("scope"), list) else []
        validation = self.validate_access_token(access_token)
        if validation.get("login"):
            state["login"] = sanitize_chat_user(validation.get("login"))
        self.history.set_secret_setting(TWITCH_OAUTH_KEY, state)
        return state

    def _post_form(self, url: str, fields: dict[str, str]) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(128 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(128 * 1024).decode("utf-8", errors="replace")
            raise RuntimeError(detail or str(exc)) from exc
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Twitch returned an invalid response.")
        return payload


class PasswordDialog(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, first_run: bool) -> None:
        super().__init__(master)
        self.result: str | None = None
        self.first_run = first_run

        self.title("Unlock TwitchAudio")
        self.geometry("440x360" if first_run else "440x300")
        self.resizable(False, False)
        self.configure(fg_color="#090b13")
        self._grab_attempts = 0
        self.after(0, self._safe_grab)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        title = "Create encrypted history vault" if first_run else "Unlock encrypted history"
        body = (
            "Choose a password for saved stream history. The password cannot be recovered."
            if first_run
            else "Enter the password for your saved stream history."
        )

        panel = ctk.CTkFrame(self, fg_color="#121625", corner_radius=22, border_width=1, border_color="#24304a")
        panel.pack(fill="both", expand=True, padx=22, pady=22)

        ctk.CTkLabel(
            panel,
            text="TwitchAudio",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#8cbcff",
        ).pack(anchor="w", padx=24, pady=(24, 2))
        ctk.CTkLabel(
            panel,
            text=title,
            font=ctk.CTkFont(size=25, weight="bold"),
            text_color="#f6f8ff",
        ).pack(anchor="w", padx=24)
        ctk.CTkLabel(
            panel,
            text=body,
            font=ctk.CTkFont(size=13),
            text_color="#a7b0c8",
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=24, pady=(8, 18))

        self.password_entry = ctk.CTkEntry(panel, placeholder_text="Password", show="*", height=42)
        self.password_entry.pack(fill="x", padx=24)
        self.password_entry.bind("<Return>", lambda _event: self._unlock())

        self.confirm_entry: ctk.CTkEntry | None = None
        if first_run:
            self.confirm_entry = ctk.CTkEntry(panel, placeholder_text="Confirm password", show="*", height=42)
            self.confirm_entry.pack(fill="x", padx=24, pady=(10, 0))
            self.confirm_entry.bind("<Return>", lambda _event: self._unlock())

        self.error_label = ctk.CTkLabel(panel, text="", text_color="#ff7a90", font=ctk.CTkFont(size=12))
        self.error_label.pack(anchor="w", padx=24, pady=(10, 0))

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(14, 0))
        ctk.CTkButton(actions, text="Cancel", fg_color="#26304a", hover_color="#34405f", command=self._cancel).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        ctk.CTkButton(actions, text="Unlock" if not first_run else "Create Vault", command=self._unlock).pack(
            side="left", fill="x", expand=True, padx=(8, 0)
        )

        self.after(100, self._focus_password)

    def _focus_password(self) -> None:
        self.lift()
        self.password_entry.focus_set()

    def _safe_grab(self) -> None:
        if self._grab_attempts >= 10:
            return
        self._grab_attempts += 1
        try:
            self.update_idletasks()
            self.wait_visibility()
            self.grab_set()
        except Exception:
            self.after(50, self._safe_grab)

    def _unlock(self) -> None:
        password = self.password_entry.get()
        if len(password) < 8:
            self.error_label.configure(text="Use at least 8 characters.")
            return

        if self.first_run and self.confirm_entry and password != self.confirm_entry.get():
            self.error_label.configure(text="Passwords do not match.")
            return

        self.result = password
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class PasswordChangeDialog(PasswordDialog):
    def __init__(self, master: ctk.CTk) -> None:
        super().__init__(master, first_run=True)
        self.title("Change History Password")
        for widget in self.winfo_children():
            widget.destroy()

        self.result = None
        self.configure(fg_color="#090b13")

        panel = ctk.CTkFrame(self, fg_color="#121625", corner_radius=22, border_width=1, border_color="#24304a")
        panel.pack(fill="both", expand=True, padx=22, pady=22)

        ctk.CTkLabel(
            panel,
            text="Rotate Vault Key",
            font=ctk.CTkFont(size=25, weight="bold"),
            text_color="#f6f8ff",
        ).pack(anchor="w", padx=24, pady=(24, 2))
        ctk.CTkLabel(
            panel,
            text="Set a new password for future launches. Existing saved streams will be re-encrypted.",
            font=ctk.CTkFont(size=13),
            text_color="#a7b0c8",
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=24, pady=(8, 18))

        self.password_entry = ctk.CTkEntry(panel, placeholder_text="New password", show="*", height=42)
        self.password_entry.pack(fill="x", padx=24)
        self.confirm_entry = ctk.CTkEntry(panel, placeholder_text="Confirm new password", show="*", height=42)
        self.confirm_entry.pack(fill="x", padx=24, pady=(10, 0))
        self.password_entry.bind("<Return>", lambda _event: self._unlock())
        self.confirm_entry.bind("<Return>", lambda _event: self._unlock())

        self.error_label = ctk.CTkLabel(panel, text="", text_color="#ff7a90", font=ctk.CTkFont(size=12))
        self.error_label.pack(anchor="w", padx=24, pady=(10, 0))

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(14, 0))
        ctk.CTkButton(actions, text="Cancel", fg_color="#26304a", hover_color="#34405f", command=self._cancel).pack(
            side="left", fill="x", expand=True, padx=(0, 8)
        )
        ctk.CTkButton(actions, text="Change Password", command=self._unlock).pack(
            side="left", fill="x", expand=True, padx=(8, 0)
        )
        self.after(100, self._focus_password)


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, history: EncryptedHistoryStore) -> None:
        super().__init__(master)
        self.history = history
        self.oauth = TwitchOAuthManager(history)
        self.auth_stop_event: threading.Event | None = None

        self.title("TwitchAudio Settings")
        self.geometry("640x680")
        self.resizable(False, False)
        self.configure(fg_color="#090b13")
        self._grab_attempts = 0
        self.after(0, self._safe_grab)
        self.protocol("WM_DELETE_WINDOW", self._close)

        state = self.oauth.get_state()
        client_id = str(state.get("client_id") or "")
        has_secret = bool(state.get("client_secret"))
        login = str(state.get("login") or "")

        panel = ctk.CTkFrame(self, fg_color="#121625", corner_radius=22, border_width=1, border_color="#24304a")
        panel.pack(fill="both", expand=True, padx=22, pady=22)

        ctk.CTkLabel(
            panel,
            text="Secure Twitch Login",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color="#f6f8ff",
        ).pack(anchor="w", padx=24, pady=(24, 4))
        ctk.CTkLabel(
            panel,
            text="Save your Twitch Client ID and Client Secret. TwitchAudio stores them, OAuth tokens, refresh tokens, and chat transcripts as AES-GCM encrypted vault payloads.",
            font=ctk.CTkFont(size=13),
            text_color="#a7b0c8",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=24, pady=(0, 18))

        ctk.CTkLabel(panel, text="Twitch Client ID", text_color="#dbe3ff", anchor="w").pack(
            fill="x", padx=24, pady=(0, 6)
        )
        self.client_id_entry = ctk.CTkEntry(panel, placeholder_text="Client ID", height=42)
        self.client_id_entry.pack(fill="x", padx=24)
        self.client_id_entry.insert(0, client_id)

        ctk.CTkLabel(panel, text="Twitch Client Secret", text_color="#dbe3ff", anchor="w").pack(
            fill="x", padx=24, pady=(16, 6)
        )
        self.client_secret_entry = ctk.CTkEntry(panel, placeholder_text="Client Secret", show="*", height=42)
        self.client_secret_entry.pack(fill="x", padx=24)
        if has_secret:
            self.client_secret_entry.configure(
                placeholder_text="Saved secret is encrypted. Enter a new secret only to replace it."
            )

        self.show_secret_var = BooleanVar(value=False)
        ctk.CTkCheckBox(
            panel,
            text="Show secret while editing",
            variable=self.show_secret_var,
            command=self.toggle_secret_visibility,
        ).pack(anchor="w", padx=24, pady=(10, 0))

        self.status_label = ctk.CTkLabel(
            panel,
            text=f"Authorized as {login}" if login else "Not authorized yet",
            text_color="#72f2c7" if login else "#ffb86c",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=24, pady=(16, 0))

        code_box = ctk.CTkFrame(panel, fg_color="#080a12", corner_radius=12, border_width=1, border_color="#24304a")
        code_box.pack(fill="x", padx=24, pady=(12, 0))
        self.code_label = ctk.CTkLabel(
            code_box,
            text="Generate a Twitch login code after saving credentials.",
            font=ctk.CTkFont(size=13),
            text_color="#dbe3ff",
            wraplength=540,
            justify="left",
        )
        self.code_label.pack(anchor="w", padx=16, pady=14)

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(fill="x", padx=24, pady=(18, 0))
        ctk.CTkButton(actions, text="Save App", command=self.save_app).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(actions, text="Generate Token", command=self.generate_token).pack(
            side="left", fill="x", expand=True, padx=8
        )
        ctk.CTkButton(
            actions,
            text="Clear",
            fg_color="#3b1d2a",
            hover_color="#5a263b",
            command=self.clear,
        ).pack(side="left", fill="x", expand=True, padx=(8, 0))

        ctk.CTkButton(
            panel,
            text="Close",
            fg_color="#26304a",
            hover_color="#34405f",
            command=self._close,
        ).pack(fill="x", padx=24, pady=(16, 0))

        ctk.CTkLabel(
            panel,
            text="Device login works even if this machine cannot open a browser: use the displayed URL and code on any browser-capable device. Chat messages are sanitized before display and encrypted before SQLite storage.",
            font=ctk.CTkFont(size=12),
            text_color="#6f7a92",
            wraplength=560,
            justify="left",
        ).pack(anchor="w", padx=24, pady=(18, 0))

        self.after(100, self.client_id_entry.focus_set)

    def _safe_grab(self) -> None:
        if self._grab_attempts >= 10:
            return
        self._grab_attempts += 1
        try:
            self.update_idletasks()
            self.wait_visibility()
            self.grab_set()
        except Exception:
            self.after(50, self._safe_grab)

    def toggle_secret_visibility(self) -> None:
        self.client_secret_entry.configure(show="" if self.show_secret_var.get() else "*")

    def save_app(self) -> bool:
        client_id = sanitize_text(self.client_id_entry.get(), max_chars=128)
        client_secret = sanitize_text(self.client_secret_entry.get(), max_chars=256)
        state = self.oauth.get_state()
        if not re.fullmatch(r"[A-Za-z0-9_]{10,128}", client_id):
            self.status_label.configure(text="Enter a valid Twitch Client ID.", text_color="#ff7a90")
            return False
        if not client_secret and state.get("client_secret"):
            client_secret = str(state.get("client_secret"))
        if len(client_secret) < 10:
            self.status_label.configure(text="Enter your Twitch Client Secret.", text_color="#ff7a90")
            return False
        self.oauth.save_app_credentials(client_id, client_secret)
        self.client_secret_entry.delete(0, "end")
        self.client_secret_entry.configure(
            placeholder_text="Saved secret is encrypted. Enter a new secret only to replace it."
        )
        self.status_label.configure(text="Saved encrypted Twitch app credentials.", text_color="#72f2c7")
        return True

    def generate_token(self) -> None:
        if not self.save_app():
            return
        try:
            device = self.oauth.begin_device_flow()
        except Exception as exc:
            self.status_label.configure(text=f"Could not start Twitch login: {exc}", text_color="#ff7a90")
            return

        self.auth_stop_event = threading.Event()
        self.code_label.configure(
            text=f"Open: {device.verification_uri}\nEnter code: {device.user_code}\nWaiting for approval..."
        )
        self.status_label.configure(text="Waiting for Twitch device approval...", text_color="#8cbcff")
        worker = threading.Thread(
            target=self._poll_device_login,
            args=(device, self.auth_stop_event),
            daemon=True,
        )
        worker.start()

    def _poll_device_login(self, device: TwitchDeviceCode, stop_event: threading.Event) -> None:
        try:
            self.oauth.poll_device_flow(
                device.device_code,
                device.interval,
                device.expires_in,
                stop_event,
                lambda message: self.after(0, lambda: self.status_label.configure(text=message, text_color="#8cbcff")),
            )
        except Exception as exc:
            self.after(0, lambda: self.status_label.configure(text=f"Twitch login failed: {exc}", text_color="#ff7a90"))
            return
        self.after(0, self._token_ready)

    def _token_ready(self) -> None:
        state = self.oauth.get_state()
        login = sanitize_chat_user(state.get("login"))
        self.status_label.configure(text=f"Authorized as {login}. Tokens are encrypted.", text_color="#72f2c7")
        self.code_label.configure(text="Twitch chat login is ready. Access and refresh tokens are stored encrypted.")

    def clear(self) -> None:
        if self.auth_stop_event:
            self.auth_stop_event.set()
        self.oauth.clear()
        self.history.delete_secret_setting(CHAT_CREDENTIALS_KEY)
        self.client_id_entry.delete(0, "end")
        self.client_secret_entry.delete(0, "end")
        self.status_label.configure(text="Cleared Twitch credentials and tokens.", text_color="#ffb86c")
        self.code_label.configure(text="Generate a Twitch login code after saving credentials.")

    def _close(self) -> None:
        if self.auth_stop_event:
            self.auth_stop_event.set()
        self.destroy()


class StreamCard(ctk.CTkFrame):
    def __init__(
        self,
        master: ctk.CTkFrame,
        record: StreamRecord,
        app: "TwitchAudioApp",
        compact: bool = False,
        online_status: bool | None = None,
    ) -> None:
        super().__init__(master, fg_color="#151a2a", corner_radius=12 if compact else 18, border_width=1, border_color="#26334f")
        self.record = record
        self.app = app
        self.compact = compact

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=10 if compact else 16, pady=(10 if compact else 14, 0))

        status_color = "#72f2c7" if online_status is True else "#ff5f7a" if online_status is False else "#6f7a92"
        ctk.CTkLabel(
            header,
            text="●",
            font=ctk.CTkFont(size=13 if compact else 16, weight="bold"),
            text_color=status_color,
            width=14,
        ).pack(side="left", anchor="w", padx=(0, 4))
        ctk.CTkLabel(
            header,
            text=record.title,
            font=ctk.CTkFont(size=13 if compact else 17, weight="bold"),
            text_color="#f6f8ff",
        ).pack(side="left", anchor="w")
        if not compact:
            ctk.CTkLabel(
                header,
                text=f"{record.play_count} plays",
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color="#72f2c7",
            ).pack(side="right", anchor="e")

        if not compact:
            ctk.CTkLabel(
                self,
                text=record.url,
                font=ctk.CTkFont(size=12),
                text_color="#9aa6bf",
                anchor="w",
            ).pack(fill="x", padx=16, pady=(3, 0))
        ctk.CTkLabel(
            self,
            text=(
                f"{record.quality} | {record.volume:.1f}x | {record.play_count} plays"
                if compact
                else f"Last played: {display_time(record.last_played_at)}  |  Quality: {record.quality}  |  Volume: {record.volume:.1f}x"
            ),
            font=ctk.CTkFont(size=10 if compact else 12),
            text_color="#6f7a92",
            anchor="w",
        ).pack(fill="x", padx=10 if compact else 16, pady=(3, 8 if compact else 12))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=10 if compact else 16, pady=(0, 10 if compact else 14))
        play_state = "disabled" if online_status is False else "normal"
        ctk.CTkButton(
            actions,
            text="Play",
            width=52 if compact else 76,
            height=28 if compact else 32,
            state=play_state,
            command=lambda: app.play_record(record),
        ).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(
            actions,
            text="Load",
            width=52 if compact else 76,
            height=28 if compact else 32,
            fg_color="#26304a",
            hover_color="#34405f",
            command=lambda: app.load_record(record),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Delete",
            width=58 if compact else 76,
            height=28 if compact else 32,
            fg_color="#3b1d2a",
            hover_color="#5a263b",
            command=lambda: app.delete_record(record),
        ).pack(side="right")


class ExploreWindow(ctk.CTkToplevel):
    PINNED_CATEGORIES = (
        "Software and Game Development",
        "Science & Technology",
        "Just Chatting",
        "Music",
        "Art",
        "Makers & Crafting",
        "Food & Drink",
        "Sports",
        "Talk Shows & Podcasts",
        "Special Events",
    )

    def __init__(self, master: "TwitchAudioApp", oauth: TwitchOAuthManager) -> None:
        super().__init__(master)
        self.app = master
        self.oauth = oauth
        self.category_buttons: dict[str, ctk.CTkButton] = {}
        self.stream_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self.active_category = ""
        self.title("Explore Twitch")
        self.geometry("980x700")
        self.minsize(820, 560)
        self.configure(fg_color="#090b13")

        shell = ctk.CTkFrame(self, fg_color="#121625", corner_radius=18, border_width=1, border_color="#24304a")
        shell.pack(fill="both", expand=True, padx=18, pady=18)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            shell,
            text="Explore",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=18, pady=(18, 2))
        ctk.CTkLabel(
            shell,
            text="Live Twitch categories sorted by Twitch. Viewer counts stay hidden.",
            font=ctk.CTkFont(size=12),
            text_color="#8b96b3",
        ).grid(row=1, column=0, columnspan=2, sticky="nw", padx=18, pady=(0, 0))

        body = ctk.CTkFrame(shell, fg_color="transparent")
        body.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=18, pady=(16, 18))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        rail = ctk.CTkFrame(body, width=245, fg_color="#0d111f", corner_radius=14, border_width=1, border_color="#24304a")
        rail.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        rail.grid_propagate(False)
        rail.grid_columnconfigure(0, weight=1)
        rail.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            rail,
            text="Categories",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 8))
        self.category_frame = ctk.CTkScrollableFrame(rail, fg_color="transparent")
        self.category_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 10))

        content = ctk.CTkFrame(body, fg_color="#080a12", corner_radius=14, border_width=1, border_color="#24304a")
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(2, weight=1)
        self.category_title = ctk.CTkLabel(
            content,
            text="Loading categories...",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#f6f8ff",
            anchor="w",
        )
        self.category_title.grid(row=0, column=0, sticky="ew", padx=18, pady=(18, 2))
        self.category_subtitle = ctk.CTkLabel(
            content,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="#8b96b3",
            anchor="w",
        )
        self.category_subtitle.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 10))
        self.stream_frame = ctk.CTkScrollableFrame(content, fg_color="transparent")
        self.stream_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))

        self._render_categories(list(self.PINNED_CATEGORIES))
        self.select_category(self.PINNED_CATEGORIES[0])
        threading.Thread(target=self._load_top_categories, daemon=True).start()

    def _load_top_categories(self) -> None:
        try:
            top_categories = self.oauth.get_top_categories()
        except Exception as exc:
            self.after(0, lambda: self._set_stream_message(f"Could not load Twitch categories: {exc}"))
            return
        combined: list[str] = []
        for category in [*self.PINNED_CATEGORIES, *top_categories]:
            normalized = category.strip()
            if normalized and normalized not in combined:
                combined.append(normalized)
        self.after(0, lambda: self._render_categories(combined))

    def _render_categories(self, categories: list[str]) -> None:
        for child in self.category_frame.winfo_children():
            child.destroy()
        self.category_buttons = {}
        for category in categories:
            button = ctk.CTkButton(
                self.category_frame,
                text=category,
                height=34,
                anchor="w",
                fg_color="#151a2a" if category == self.active_category else "transparent",
                hover_color="#26304a",
                text_color="#f6f8ff" if category == self.active_category else "#a7b0c8",
                command=lambda selected=category: self.select_category(selected),
            )
            button.pack(fill="x", padx=4, pady=3)
            self.category_buttons[category] = button

    def select_category(self, category: str) -> None:
        self.active_category = category
        for name, button in self.category_buttons.items():
            button.configure(
                fg_color="#151a2a" if name == category else "transparent",
                text_color="#f6f8ff" if name == category else "#a7b0c8",
            )
        self.category_title.configure(text=category)
        self.category_subtitle.configure(text="Live now, sorted by Twitch. No viewer counts shown.")
        cached = self.stream_cache.get(category)
        if cached and time.time() - cached[0] < EXPLORE_CACHE_SECONDS:
            self._render_category(category, cached[1])
            return
        self._set_stream_message("Loading live streams...")
        threading.Thread(target=self._load_category, args=(category,), daemon=True).start()

    def _set_stream_message(self, message: str) -> None:
        for child in self.stream_frame.winfo_children():
            child.destroy()
        ctk.CTkLabel(
            self.stream_frame,
            text=message,
            text_color="#a7b0c8",
            font=ctk.CTkFont(size=13),
            wraplength=600,
            justify="left",
        ).pack(anchor="w", padx=10, pady=10)

    def _load_category(self, category: str) -> None:
        try:
            streams = self.oauth.get_category_streams(category, limit=50)
        except Exception as exc:
            self.after(0, lambda: self._set_stream_message(f"Could not load streams: {exc}"))
            return
        self.stream_cache[category] = (time.time(), streams)
        self.after(0, lambda: self._render_category(category, streams))

    def _render_category(self, category: str, streams: list[dict[str, str]]) -> None:
        if category != self.active_category:
            return
        for child in self.stream_frame.winfo_children():
            child.destroy()
        if not streams:
            self._set_stream_message("No live streams found for this category.")
            return
        for index, stream in enumerate(streams, start=1):
            card = ctk.CTkFrame(self.stream_frame, fg_color="#151a2a", corner_radius=10, border_width=1, border_color="#26334f")
            card.pack(fill="x", padx=4, pady=6)
            card.grid_columnconfigure(1, weight=1)
            rank = ctk.CTkLabel(
                card,
                text=f"{index:02d}",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#72f2c7",
                width=40,
            )
            rank.grid(row=0, column=0, rowspan=2, sticky="n", padx=(12, 4), pady=12)
            ctk.CTkLabel(
                card,
                text=stream["display_name"],
                font=ctk.CTkFont(size=16, weight="bold"),
                text_color="#f6f8ff",
                anchor="w",
            ).grid(row=0, column=1, sticky="ew", padx=(4, 12), pady=(10, 0))
            ctk.CTkLabel(
                card,
                text=stream["title"] or "Untitled live stream",
                font=ctk.CTkFont(size=12),
                text_color="#a7b0c8",
                wraplength=560,
                justify="left",
                anchor="w",
            ).grid(row=1, column=1, sticky="ew", padx=(4, 12), pady=(2, 10))
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.grid(row=0, column=2, rowspan=2, sticky="e", padx=12, pady=10)
            ctk.CTkButton(
                actions,
                text="Load",
                width=76,
                fg_color="#26304a",
                hover_color="#34405f",
                command=lambda url=stream["url"]: self._load_stream(url),
            ).pack(anchor="e", pady=(0, 6))
            ctk.CTkButton(
                actions,
                text="Play",
                width=76,
                command=lambda url=stream["url"]: self._play_stream(url),
            ).pack(anchor="e")

    def _load_stream(self, url: str) -> None:
        self.app.url_entry.delete(0, "end")
        self.app.url_entry.insert(0, url)
        if not self.app.url_entry.winfo_ismapped():
            self.app.url_entry.grid()
        self.app.lift()

    def _play_stream(self, url: str) -> None:
        self._load_stream(url)
        self.app.start_stream()


class TwitchChatReader:
    def __init__(
        self,
        channel: str,
        nick: str,
        token: str,
        event_queue: queue.Queue[tuple[str, str]],
        stop_event: threading.Event,
    ) -> None:
        self.channel = channel
        self.nick = nick
        self.token = format_chat_token(token)
        self.event_queue = event_queue
        self.stop_event = stop_event
        self.sock: ssl.SSLSocket | None = None
        self.send_lock = threading.Lock()

    def run(self) -> None:
        sock: ssl.SSLSocket | None = None
        try:
            context = ssl.create_default_context()
            raw_sock = socket.create_connection((TWITCH_IRC_HOST, TWITCH_IRC_PORT), timeout=10)
            sock = context.wrap_socket(raw_sock, server_hostname=TWITCH_IRC_HOST)
            self.sock = sock
            sock.settimeout(CHAT_SOCKET_TIMEOUT_SECONDS)

            self._send(f"PASS {self.token}")
            self._send(f"NICK {self.nick}")
            self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            self._send(f"JOIN #{self.channel}")
            self.event_queue.put(("chat_status", f"Connected to #{self.channel}"))

            buffer = ""
            while not self.stop_event.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue

                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")
                lines = buffer.split("\r\n")
                buffer = lines.pop()

                for line in lines:
                    if not line:
                        continue
                    if line.startswith("PING"):
                        self._send(line.replace("PING", "PONG", 1))
                        continue
                    parsed = parse_privmsg(line)
                    if parsed:
                        user, message = parsed
                        clean_user = sanitize_chat_user(user)
                        clean_message = sanitize_chat_message(message)
                        if clean_message:
                            self.event_queue.put(("chat_message", json.dumps({"user": clean_user, "message": clean_message})))
                    elif "Login authentication failed" in line:
                        self.event_queue.put(("chat_status", "Chat auth failed. Check Settings."))
                        return
                    elif " NOTICE " in line and " :" in line:
                        self.event_queue.put(("chat_status", "Chat notice: " + line.rsplit(" :", 1)[-1]))
        except Exception as exc:
            if not self.stop_event.is_set():
                self.event_queue.put(("chat_status", f"Chat disconnected: {exc}"))
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            self.sock = None
            if not self.stop_event.is_set():
                self.event_queue.put(("chat_status", "Chat disconnected."))

    def send_chat_message(self, message: str) -> bool:
        message = sanitize_chat_message(message)
        if not message or self.stop_event.is_set() or self.sock is None:
            return False
        self._send(f"PRIVMSG #{self.channel} :{message}")
        return True

    def _send(self, command: str) -> None:
        if self.sock is None:
            raise OSError("Chat socket is not connected.")
        with self.send_lock:
            self.sock.sendall(f"{command}\r\n".encode("utf-8"))


class TwitchAudioApp(ctk.CTk):
    def __init__(self, history: EncryptedHistoryStore) -> None:
        super().__init__()
        self.history = history
        self.stream_process: subprocess.Popen[bytes] | None = None
        self.play_process: subprocess.Popen[bytes] | None = None
        self.embedded_video_process: subprocess.Popen[bytes] | None = None
        self.video_stream_process: subprocess.Popen[bytes] | None = None
        self.video_play_process: subprocess.Popen[bytes] | None = None
        self.event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.chat_thread: threading.Thread | None = None
        self.chat_stop_event: threading.Event | None = None
        self.chat_reader: TwitchChatReader | None = None
        self.chat_channel: str | None = None
        self.pending_chat_messages: list[tuple[str, str, str, str]] = []
        self.chat_flush_after_id: str | None = None
        self.volume_restart_after_id: str | None = None
        self.suppress_volume_restart = False
        self.is_streaming = False
        self.is_video_popped = False
        self.log_window: ctk.CTkToplevel | None = None
        self.log_box: ctk.CTkTextbox | None = None
        self.log_lines: list[str] = []
        self.oauth = TwitchOAuthManager(history)
        self.online_statuses: dict[str, bool] = {}
        self.online_refresh_in_progress = False
        self.last_online_refresh = 0.0
        self.video_restart_attempts = 0
        self.video_restart_after_id: str | None = None
        self.video_last_url = ""
        self.video_last_quality = ""
        self.video_last_volume = 2.0
        self.stopping_stream = False
        self.stream_health = "Idle"
        self.process_log_tails: dict[str, list[str]] = {}
        self.diagnostic_last_emit: dict[str, float] = {}
        self.diagnostics_visible = False
        self.chat_line_count = 0
        self.fullscreen_video = False

        self.title("TwitchAudio Command Deck")
        self.geometry("1180x760")
        self.minsize(980, 650)
        self.configure(fg_color="#080a12")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self.refresh_history()
        self.log("Encrypted history vault unlocked.")
        self.after(EVENT_POLL_IDLE_MS, self.process_events)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=285, fg_color="#0d111f", corner_radius=0)
        sidebar = self.sidebar
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        self.logo_image: PhotoImage | None = None
        if LOGO_PATH.exists():
            try:
                raw_logo = PhotoImage(file=str(LOGO_PATH))
                scale = max(raw_logo.width() // 235, raw_logo.height() // 134, 1)
                self.logo_image = raw_logo.subsample(scale, scale)
                ctk.CTkLabel(sidebar, image=self.logo_image, text="").pack(anchor="w", padx=24, pady=(20, 4))
            except Exception as exc:
                self.log(f"Logo could not be loaded: {exc}")

        if not self.logo_image:
            ctk.CTkLabel(
                sidebar,
                text="TwitchFreedom",
                font=ctk.CTkFont(size=24, weight="bold"),
                text_color="#f6f8ff",
            ).pack(anchor="w", padx=26, pady=(32, 2))

        ctk.CTkButton(
            sidebar,
            text="Settings",
            fg_color="#26304a",
            hover_color="#34405f",
            command=self.open_settings,
        ).pack(fill="x", padx=22, pady=(28, 8))
        ctk.CTkButton(
            sidebar,
            text="Add Stream",
            fg_color="#26304a",
            hover_color="#34405f",
            command=self.toggle_stream_url,
        ).pack(fill="x", padx=22, pady=8)
        ctk.CTkButton(
            sidebar,
            text="Explore",
            fg_color="#26304a",
            hover_color="#34405f",
            command=self.open_explore,
        ).pack(fill="x", padx=22, pady=8)
        ctk.CTkButton(
            sidebar,
            text="Diagnostics",
            fg_color="#26304a",
            hover_color="#34405f",
            command=self.open_diagnostics,
        ).pack(fill="x", padx=22, pady=8)
        ctk.CTkButton(
            sidebar,
            text="Change Vault Password",
            fg_color="#26304a",
            hover_color="#34405f",
            command=self.change_history_password,
        ).pack(fill="x", padx=22, pady=8)
        ctk.CTkButton(
            sidebar,
            text="Clear Saved Streams",
            fg_color="#3b1d2a",
            hover_color="#5a263b",
            command=self.clear_history,
        ).pack(fill="x", padx=22, pady=8)

        history_sidebar = ctk.CTkFrame(sidebar, fg_color="#121625", corner_radius=14, border_width=1, border_color="#24304a")
        history_sidebar.pack(fill="both", expand=True, padx=22, pady=(10, 8))
        history_sidebar.grid_columnconfigure(0, weight=1)
        history_sidebar.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            history_sidebar,
            text="Encrypted History",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        self.history_frame = ctk.CTkScrollableFrame(history_sidebar, fg_color="transparent")
        self.history_frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        ctk.CTkLabel(
            sidebar,
            text=f"Vault: {DB_PATH}",
            font=ctk.CTkFont(size=11),
            text_color="#5f6980",
            wraplength=235,
            justify="left",
        ).pack(side="bottom", anchor="w", padx=24, pady=24)

        self.main = ctk.CTkFrame(self, fg_color="#080a12", corner_radius=0)
        main = self.main
        main.grid(row=0, column=1, sticky="nsew", padx=26, pady=26)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        self.hero = ctk.CTkFrame(main, fg_color="#121625", corner_radius=24, border_width=1, border_color="#24304a")
        hero = self.hero
        hero.grid(row=0, column=0, sticky="ew")
        hero.grid_columnconfigure(0, weight=1)
        hero.grid_columnconfigure(1, weight=0)

        ctk.CTkLabel(
            hero,
            text="Live Stream Control",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(12, 0))
        ctk.CTkLabel(
            hero,
            text="Twitch stream controls",
            font=ctk.CTkFont(size=11),
            text_color="#8b96b3",
        ).grid(row=1, column=0, sticky="w", padx=18, pady=(2, 10))

        self.status_card = ctk.CTkFrame(hero, width=245, height=76, fg_color="#151a2a", corner_radius=14, border_width=1, border_color="#26334f")
        self.status_card.grid(row=0, column=1, rowspan=2, sticky="ne", padx=18, pady=(12, 4))
        self.status_card.grid_propagate(False)
        self.status_label = ctk.CTkLabel(
            self.status_card,
            text="Ready",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#72f2c7",
            anchor="w",
        )
        self.status_label.pack(fill="x", padx=16, pady=(12, 2))
        self.now_playing_label = ctk.CTkLabel(
            self.status_card,
            text="No active stream",
            font=ctk.CTkFont(size=12),
            text_color="#a7b0c8",
            wraplength=210,
            justify="left",
            anchor="w",
        )
        self.now_playing_label.pack(fill="x", padx=16, pady=(0, 12))

        controls = ctk.CTkFrame(hero, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=18, pady=(0, 14))
        controls.grid_columnconfigure(3, weight=1)

        self.url_entry = ctk.CTkEntry(controls, height=48, placeholder_text="https://www.twitch.tv/channel")
        self.url_entry.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 14))
        self.url_entry.insert(0, DEFAULT_STREAM_URL)
        self.url_entry.grid_remove()

        self.playback_mode_option = ctk.CTkOptionMenu(
            controls,
            values=[PLAYBACK_AUDIO_ONLY, PLAYBACK_LOW_VIDEO],
            height=40,
            command=self.on_playback_mode_changed,
        )
        self.playback_mode_option.set(PLAYBACK_AUDIO_ONLY)
        self.playback_mode_option.grid(row=1, column=0, sticky="w")

        self.quality_option = ctk.CTkOptionMenu(controls, values=[QUALITY_AUDIO_ONLY], height=40)
        self.quality_option.set(QUALITY_AUDIO_ONLY)
        self.quality_option.grid(row=1, column=1, sticky="w", padx=(12, 0))

        self.volume_slider = ctk.CTkSlider(controls, from_=0.5, to=3.0, number_of_steps=25, command=self._update_volume_label)
        self.volume_slider.grid(row=1, column=2, sticky="ew", padx=16)
        self.volume_label = ctk.CTkLabel(controls, text="Volume 2.0x", width=100, text_color="#a7b0c8")
        self.volume_label.grid(row=1, column=3, sticky="e")
        self.volume_slider.set(2.0)
        self._update_volume_label(2.0)

        actions = ctk.CTkFrame(hero, fg_color="transparent")
        actions.grid(row=2, column=1, sticky="e", padx=18, pady=(0, 14))
        self.start_button = ctk.CTkButton(actions, text="Start Audio", width=118, height=38, command=self.start_stream)
        self.start_button.pack(side="left", padx=(0, 12))
        self.stop_button = ctk.CTkButton(
            actions,
            text="Stop",
            width=84,
            height=38,
            fg_color="#3b1d2a",
            hover_color="#5a263b",
            state="disabled",
            command=lambda: self.stop_stream(user_requested=True),
        )
        self.stop_button.pack(side="left")

        self.content = ctk.CTkFrame(main, fg_color="transparent")
        content = self.content
        content.grid(row=2, column=0, sticky="nsew", pady=(22, 0))
        content.grid_columnconfigure(0, weight=3)
        content.grid_columnconfigure(1, weight=2)
        content.grid_rowconfigure(0, weight=1)

        self.video_shell = ctk.CTkFrame(content, fg_color="#121625", corner_radius=24, border_width=1, border_color="#24304a")
        video_shell = self.video_shell
        video_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        video_shell.grid_rowconfigure(1, weight=1)
        video_shell.grid_columnconfigure(0, weight=1)

        self.video_title_label = ctk.CTkLabel(
            video_shell,
            text="Video",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#f6f8ff",
        )
        self.video_title_label.grid(row=0, column=0, sticky="w", padx=22, pady=(22, 8))

        self.video_panel = ctk.CTkFrame(video_shell, fg_color="#080a12", corner_radius=14, border_width=1, border_color="#24304a")
        video_panel = self.video_panel
        video_panel.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        video_panel.grid_columnconfigure(0, weight=1)
        video_panel.grid_rowconfigure(1, weight=1)
        self.video_status_label = ctk.CTkLabel(
            video_panel,
            text=VIDEO_READY_MESSAGE,
            font=ctk.CTkFont(size=13),
            text_color="#a7b0c8",
            wraplength=640,
            justify="left",
        )
        self.video_status_label.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))
        self.video_surface = ctk.CTkFrame(video_panel, fg_color="#000000", corner_radius=8)
        self.video_surface.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))
        self.video_surface.bind("<Double-Button-1>", self.toggle_embedded_fullscreen)
        video_placeholder = ctk.CTkLabel(
            self.video_surface,
            text="Double-click for fullscreen",
            text_color="#6f7a92",
        )
        video_placeholder.pack(expand=True)
        video_placeholder.bind("<Double-Button-1>", self.toggle_embedded_fullscreen)
        self.video_hint_label = ctk.CTkLabel(
            video_panel,
            text="Start Video embeds here with mpv. Player Window uses streamlink with a separate ffplay window.",
            font=ctk.CTkFont(size=12),
            text_color="#6f7a92",
            wraplength=640,
            justify="left",
        )
        self.video_hint_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.pop_video_button = ctk.CTkButton(
            video_panel,
            text="Start Player Window",
            height=40,
            fg_color="#26304a",
            hover_color="#34405f",
            command=self.toggle_video_popout,
        )
        self.pop_video_button.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        self.stream_health_label = ctk.CTkLabel(
            video_shell,
            text="Health: Idle",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#7f8aa6",
            anchor="w",
        )
        self.stream_health_label.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 16))
        self.bind("<Escape>", self.exit_embedded_fullscreen)

        self.side_stack = ctk.CTkFrame(content, fg_color="transparent")
        side_stack = self.side_stack
        side_stack.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        side_stack.grid_columnconfigure(0, weight=1)
        side_stack.grid_rowconfigure(0, weight=1)

        chat_shell = ctk.CTkFrame(side_stack, fg_color="#121625", corner_radius=24, border_width=1, border_color="#24304a")
        chat_shell.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        chat_shell.grid_rowconfigure(2, weight=1)
        chat_shell.grid_columnconfigure(0, weight=1)

        chat_header = ctk.CTkFrame(chat_shell, fg_color="transparent")
        chat_header.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 8))
        chat_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            chat_header,
            text="Twitch Chat",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, sticky="w")
        self.chat_status_label = ctk.CTkLabel(
            chat_header,
            text="Disconnected",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#7f8aa6",
        )
        self.chat_status_label.grid(row=0, column=1, sticky="e")

        chat_actions = ctk.CTkFrame(chat_shell, fg_color="transparent")
        chat_actions.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        ctk.CTkButton(chat_actions, text="Connect", width=88, command=self.connect_chat).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            chat_actions,
            text="Disconnect",
            width=96,
            fg_color="#26304a",
            hover_color="#34405f",
            command=lambda: self.disconnect_chat(user_requested=True),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            chat_actions,
            text="Open Popout",
            width=110,
            fg_color="#26304a",
            hover_color="#34405f",
            command=self.open_chat_popout,
        ).pack(side="right")

        self.chat_box = ctk.CTkTextbox(chat_shell, fg_color="#080a12", text_color="#f6f8ff", border_width=0, wrap="word")
        self.chat_box.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 10))
        self.chat_box.insert(
            "end",
            "Open Settings to save encrypted Twitch chat credentials.\n"
            "Or click Open Popout to view chat in your browser without app chat auth.\n",
        )
        self.chat_line_count = 2
        self.chat_box.configure(state="disabled")

        chat_send = ctk.CTkFrame(chat_shell, fg_color="transparent")
        chat_send.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        chat_send.grid_columnconfigure(0, weight=1)
        self.chat_entry = ctk.CTkEntry(chat_send, placeholder_text="Send a chat message...", height=38)
        self.chat_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.chat_entry.bind("<Return>", lambda _event: self.send_chat_message())
        ctk.CTkButton(chat_send, text="Send", width=70, command=self.send_chat_message).grid(row=0, column=1, sticky="e")

    def use_default_stream(self) -> None:
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, DEFAULT_STREAM_URL)
        self.log("Loaded BeardHero preset.")

    def toggle_stream_url(self) -> None:
        if self.url_entry.winfo_ismapped():
            self.url_entry.grid_remove()
            return
        self.url_entry.grid()
        self.url_entry.focus_set()

    def open_settings(self) -> None:
        dialog = SettingsDialog(self, self.history)
        self.wait_window(dialog)

    def open_explore(self) -> None:
        ExploreWindow(self, self.oauth)

    def open_diagnostics(self) -> None:
        if self.log_window and self.log_window.winfo_exists():
            self.log_window.lift()
            return

        self.log_window = ctk.CTkToplevel(self)
        self.diagnostics_visible = True
        self.log_window.title("TwitchAudio Diagnostics")
        self.log_window.geometry("720x460")
        self.log_window.configure(fg_color="#090b13")
        self.log_window.protocol("WM_DELETE_WINDOW", self._close_diagnostics)

        shell = ctk.CTkFrame(self.log_window, fg_color="#121625", corner_radius=18, border_width=1, border_color="#24304a")
        shell.pack(fill="both", expand=True, padx=18, pady=18)
        ctk.CTkLabel(
            shell,
            text="Diagnostics",
            font=ctk.CTkFont(size=24, weight="bold"),
            text_color="#f6f8ff",
        ).pack(anchor="w", padx=18, pady=(18, 10))
        self.log_box = ctk.CTkTextbox(shell, fg_color="#080a12", text_color="#dbe3ff", border_width=0, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.log_box.configure(state="normal")
        self.log_box.insert("end", "".join(self.log_lines))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _close_diagnostics(self) -> None:
        self.diagnostics_visible = False
        if self.log_window:
            self.log_window.destroy()
        self.log_window = None
        self.log_box = None

    def on_playback_mode_changed(self, mode: str) -> None:
        if mode == PLAYBACK_LOW_VIDEO:
            self.quality_option.configure(values=list(LOW_VIDEO_QUALITIES))
            if self.quality_option.get() not in LOW_VIDEO_QUALITIES:
                self.quality_option.set(LOW_VIDEO_QUALITIES[0])
            self.start_button.configure(text="Start Low Video")
            self.log("Video mode selected.")
            return

        self.quality_option.configure(values=[QUALITY_AUDIO_ONLY])
        self.quality_option.set(QUALITY_AUDIO_ONLY)
        self.start_button.configure(text="Start Audio")

    def _update_volume_label(self, value: float) -> None:
        volume = float(value)
        self.volume_label.configure(text=f"Volume {volume:.1f}x")
        if self.is_streaming and not self.suppress_volume_restart:
            self.schedule_volume_restart(volume)

    def schedule_volume_restart(self, volume: float) -> None:
        if self.volume_restart_after_id is not None:
            self.after_cancel(self.volume_restart_after_id)

        self.volume_label.configure(text=f"Volume {volume:.1f}x queued")
        self.volume_restart_after_id = self.after(800, self.restart_stream_for_volume)

    def cancel_volume_restart(self) -> None:
        if self.volume_restart_after_id is not None:
            self.after_cancel(self.volume_restart_after_id)
            self.volume_restart_after_id = None

    def restart_stream_for_volume(self) -> None:
        self.volume_restart_after_id = None
        if not self.is_streaming:
            return

        volume = float(self.volume_slider.get())
        self.volume_label.configure(text=f"Volume {volume:.1f}x applying")
        self.log(f"Applying volume {volume:.1f}x by restarting the audio pipe.")
        self.stop_stream(user_requested=False)
        if self.start_stream(count_play=False):
            self.volume_label.configure(text=f"Volume {volume:.1f}x")

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-500:]
        if self.log_box and self.log_box.winfo_exists():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line)
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

    def add_chat_line(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        message = sanitize_chat_message(message)
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", f"[{timestamp}] {message}\n")
        self.chat_line_count += 1
        if self.chat_line_count > MAX_CHAT_LINES + CHAT_UI_TRIM_BATCH:
            self.chat_box.delete("1.0", f"{CHAT_UI_TRIM_BATCH + 1}.0")
            self.chat_line_count -= CHAT_UI_TRIM_BATCH
        self.chat_box.see("end")
        self.chat_box.configure(state="disabled")

    def queue_chat_save(self, channel: str, user: str, message: str, direction: str) -> None:
        self.pending_chat_messages.append((channel, user, message, direction))
        if self.chat_flush_after_id is None:
            self.chat_flush_after_id = self.after(CHAT_SAVE_BATCH_MS, self.flush_chat_saves)

    def flush_chat_saves(self) -> None:
        self.chat_flush_after_id = None
        if not self.pending_chat_messages:
            return
        messages = self.pending_chat_messages
        self.pending_chat_messages = []
        try:
            self.history.save_chat_messages(messages)
        except Exception as exc:
            self.log(f"Chat transcript save failed: {exc}")

    def load_chat_history(self, channel: str) -> None:
        records = self.history.list_chat_messages(channel)
        if not records:
            return
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", f"Loaded encrypted chat transcript for #{channel}.\n")
        for record in records:
            timestamp = display_time(record.created_at)
            prefix = "You" if record.direction == "out" else record.user
            self.chat_box.insert("end", f"[{timestamp}] {prefix}: {record.message}\n")
        self.chat_line_count += len(records) + 1
        if self.chat_line_count > MAX_CHAT_LINES + CHAT_UI_TRIM_BATCH:
            extra_batches = max(1, (self.chat_line_count - MAX_CHAT_LINES) // CHAT_UI_TRIM_BATCH)
            lines_to_delete = extra_batches * CHAT_UI_TRIM_BATCH
            self.chat_box.delete("1.0", f"{lines_to_delete + 1}.0")
            self.chat_line_count -= lines_to_delete
        self.chat_box.see("end")
        self.chat_box.configure(state="disabled")

    def set_chat_status(self, message: str, color: str = "#7f8aa6") -> None:
        self.chat_status_label.configure(text=message, text_color=color)
        self.add_chat_line(message)

    def connect_chat(self) -> None:
        if self.chat_thread and self.chat_thread.is_alive():
            self.set_chat_status("Already connected", "#72f2c7")
            return

        channel = twitch_channel_from_url(self.url_entry.get())
        if not channel:
            messagebox.showerror("Chat channel missing", "Enter a Twitch channel URL before connecting chat.")
            return

        try:
            nick, token = TwitchOAuthManager(self.history).get_chat_identity()
        except Exception as exc:
            self.set_chat_status("Chat auth not configured", "#ffb86c")
            messagebox.showinfo(
                "Chat settings required",
                "Open Settings, save your Twitch Client ID and Client Secret, then generate a Twitch chat token.\n\n"
                f"Detail: {exc}",
            )
            return

        self.disconnect_chat(user_requested=False)
        self.chat_channel = channel
        self.load_chat_history(channel)
        self.chat_stop_event = threading.Event()
        self.chat_reader = TwitchChatReader(channel, nick, token, self.event_queue, self.chat_stop_event)
        self.chat_thread = threading.Thread(target=self.chat_reader.run, daemon=True)
        self.chat_thread.start()
        self.set_chat_status(f"Connecting to #{channel}...", "#8cbcff")

    def disconnect_chat(self, user_requested: bool) -> None:
        if self.chat_stop_event:
            self.chat_stop_event.set()
        self.chat_stop_event = None
        self.chat_thread = None
        self.chat_reader = None
        self.chat_channel = None
        self.chat_status_label.configure(text="Disconnected", text_color="#7f8aa6")
        if user_requested:
            self.add_chat_line("Chat disconnected.")

    def send_chat_message(self) -> None:
        message = sanitize_chat_message(self.chat_entry.get())
        if not message:
            return
        if not self.chat_reader or not self.chat_thread or not self.chat_thread.is_alive():
            self.set_chat_status("Connect chat before sending.", "#ffb86c")
            return
        try:
            if not self.chat_reader.send_chat_message(message):
                self.set_chat_status("Chat is not ready to send yet.", "#ffb86c")
                return
        except OSError as exc:
            self.set_chat_status(f"Send failed: {exc}", "#ffb86c")
            return

        self.chat_entry.delete(0, "end")
        if self.chat_channel:
            self.queue_chat_save(self.chat_channel, "You", message, "out")
        self.add_chat_line(f"You: {message}")

    def open_chat_popout(self) -> None:
        channel = twitch_channel_from_url(self.url_entry.get())
        if not channel:
            messagebox.showerror("Chat channel missing", "Enter a Twitch channel URL before opening chat.")
            return
        webbrowser.open(f"https://www.twitch.tv/popout/{channel}/chat?popout=", new=2)
        self.add_chat_line(f"Opened browser chat popout for #{channel}.")

    def refresh_history(self, check_online: bool = True) -> None:
        for child in self.history_frame.winfo_children():
            child.destroy()

        records = self.history.list_streams()
        if not records:
            empty = ctk.CTkFrame(self.history_frame, fg_color="#151a2a", corner_radius=18, border_width=1, border_color="#26334f")
            empty.pack(fill="x", padx=2, pady=4)
            ctk.CTkLabel(
                empty,
                text="No saved streams yet",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#f6f8ff",
            ).pack(anchor="w", padx=10, pady=(10, 4))
            ctk.CTkLabel(
                empty,
                text="Streams save here encrypted.",
                font=ctk.CTkFont(size=11),
                text_color="#9aa6bf",
                wraplength=200,
                justify="left",
            ).pack(anchor="w", padx=10, pady=(0, 10))
            return

        for record in records:
            channel = twitch_channel_from_url(record.url) or ""
            status = self.online_statuses.get(channel.lower())
            card = StreamCard(self.history_frame, record, self, compact=True, online_status=status)
            card.pack(fill="x", padx=2, pady=6)
        if check_online:
            self.refresh_online_statuses(records)

    def refresh_online_statuses(self, records: list[StreamRecord] | None = None) -> None:
        if self.is_streaming or self.is_video_popped:
            return
        if self.online_refresh_in_progress or time.time() - self.last_online_refresh < ONLINE_STATUS_REFRESH_SECONDS:
            return
        records = records if records is not None else self.history.list_streams()
        channels = sorted({channel.lower() for record in records if (channel := twitch_channel_from_url(record.url))})
        if not channels:
            return
        self.online_refresh_in_progress = True
        self.last_online_refresh = time.time()
        threading.Thread(target=self._load_online_statuses, args=(channels,), daemon=True).start()

    def _load_online_statuses(self, channels: list[str]) -> None:
        try:
            online = self.oauth.get_online_channels(channels)
        except Exception as exc:
            self.event_queue.put(("online_error", f"Online status unavailable: {exc}"))
            return
        statuses = {channel.lower(): channel.lower() in online for channel in channels}
        self.event_queue.put(("online_statuses", json.dumps(statuses)))

    def _streamlink_command(self, url: str, quality: str) -> list[str]:
        return [
            "streamlink",
            "--loglevel",
            "warning",
            "--stdout",
            "--twitch-disable-ads",
            "--stream-segment-threads",
            STREAMLINK_SEGMENT_THREADS,
            "--stream-segment-attempts",
            "10",
            "--stream-segment-timeout",
            "20",
            "--hls-live-edge",
            "8",
            "--hls-playlist-reload-attempts",
            "15",
            "--retry-streams",
            "10",
            "--retry-open",
            "10",
            "--ringbuffer-size",
            STREAMLINK_RINGBUFFER_SIZE,
            url,
            quality,
        ]

    def _ffplay_command(self, volume: float) -> list[str]:
        return [
            "ffplay",
            "-autoexit",
            "-nodisp",
            "-f",
            "mpegts",
            "-af",
            f"volume={volume:.1f}",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-",
        ]

    def _ffplay_video_command(self, volume: float) -> list[str]:
        return [
            "ffplay",
            "-autoexit",
            "-f",
            "mpegts",
            "-af",
            f"volume={volume:.1f}",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-",
        ]

    def _embedded_mpv_command(self, volume: float) -> list[str]:
        self.update_idletasks()
        window_id = str(self.video_surface.winfo_id())
        return [
            "mpv",
            "--no-config",
            f"--wid={window_id}",
            "--cache=yes",
            f"--cache-secs={VIDEO_CACHE_SECONDS}",
            f"--demuxer-readahead-secs={VIDEO_CACHE_SECONDS}",
            "--demuxer-max-bytes=512MiB",
            "--demuxer-max-back-bytes=128MiB",
            "--vd-lavc-threads=4",
            "--vd-lavc-fast=yes",
            "--vd-lavc-skiploopfilter=nonref",
            "--force-window=yes",
            "--vo=x11",
            "--hwdec=auto-safe",
            "--framedrop=decoder+vo",
            "--osc=no",
            "--osd-level=0",
            "--audio-display=no",
            "--sws-fast=yes",
            "--sws-scaler=fast-bilinear",
            "--untimed=no",
            "--video-sync=display-resample",
            "--input-default-bindings=no",
            "--input-vo-keyboard=no",
            "--demuxer-lavf-format=mpegts",
            f"--volume={max(0, min(volume * 100, 300)):.0f}",
            "-",
        ]

    def _start_stderr_drain(self, process: subprocess.Popen[bytes] | None, name: str) -> None:
        if process is None or process.stderr is None:
            return
        self.process_log_tails[name] = []

        def drain() -> None:
            assert process.stderr is not None
            while True:
                try:
                    raw = process.stderr.readline()
                except Exception:
                    return
                if not raw:
                    return
                line = sanitize_text(raw.decode("utf-8", errors="replace"), max_chars=1200)
                if not line:
                    continue
                tail = self.process_log_tails.setdefault(name, [])
                tail.append(line)
                del tail[:-25]
                now = time.time()
                if self.diagnostics_visible and now - self.diagnostic_last_emit.get(name, 0.0) >= DIAGNOSTIC_LOG_INTERVAL_SECONDS:
                    self.diagnostic_last_emit[name] = now
                    self.event_queue.put(("diagnostic", f"{name}: {line}"))

        threading.Thread(target=drain, daemon=True).start()

    def _process_error_tail(self, process: subprocess.Popen[bytes] | None, name: str | None = None) -> str:
        if name and self.process_log_tails.get(name):
            return "\n".join(self.process_log_tails[name])[-1200:]
        if process is None or process.stderr is None:
            return ""
        try:
            raw = process.stderr.read(4096)
        except Exception:
            return ""
        return raw.decode("utf-8", errors="replace").strip()[-1200:]

    def toggle_embedded_fullscreen(self, _event: object | None = None) -> None:
        if self.fullscreen_video:
            self.exit_embedded_fullscreen()
            return
        self.fullscreen_video = True
        self._set_video_only_fullscreen(True)
        self.attributes("-fullscreen", True)
        self.video_status_label.configure(text="Fullscreen enabled. Press Esc or double-click the video area to exit.")
        self.log("Video fullscreen enabled.")

    def exit_embedded_fullscreen(self, _event: object | None = None) -> None:
        if not self.fullscreen_video:
            return
        self.fullscreen_video = False
        self.attributes("-fullscreen", False)
        self._set_video_only_fullscreen(False)
        self.video_status_label.configure(text=VIDEO_READY_MESSAGE, text_color="#a7b0c8")
        self.log("Video fullscreen disabled.")

    def _set_video_only_fullscreen(self, enabled: bool) -> None:
        if enabled:
            self.sidebar.grid_remove()
            self.hero.grid_remove()
            self.side_stack.grid_remove()
            self.video_title_label.grid_remove()
            self.video_status_label.grid_remove()
            self.video_hint_label.grid_remove()
            self.pop_video_button.grid_remove()
            self.stream_health_label.grid_remove()

            self.main.grid_configure(row=0, column=0, columnspan=2, sticky="nsew", padx=0, pady=0)
            self.main.grid_rowconfigure(0, weight=1)
            self.main.grid_rowconfigure(2, weight=0)
            self.content.grid_configure(row=0, column=0, sticky="nsew", pady=0)
            self.content.grid_columnconfigure(0, weight=1)
            self.content.grid_columnconfigure(1, weight=0)
            self.video_shell.grid_configure(row=0, column=0, sticky="nsew", padx=0)
            self.video_shell.grid_rowconfigure(0, weight=1)
            self.video_shell.grid_rowconfigure(1, weight=0)
            self.video_shell.configure(fg_color="#000000", corner_radius=0, border_width=0)
            self.video_panel.grid_configure(row=0, column=0, sticky="nsew", padx=0, pady=0)
            self.video_panel.grid_rowconfigure(0, weight=1)
            self.video_panel.grid_rowconfigure(1, weight=0)
            self.video_panel.configure(fg_color="#000000", corner_radius=0, border_width=0)
            self.video_surface.grid_configure(row=0, column=0, sticky="nsew", padx=0, pady=0)
            self.video_surface.configure(corner_radius=0)
        else:
            self.main.grid_configure(row=0, column=1, columnspan=1, sticky="nsew", padx=26, pady=26)
            self.main.grid_rowconfigure(0, weight=0)
            self.main.grid_rowconfigure(2, weight=1)
            self.hero.grid(row=0, column=0, sticky="ew")
            self.content.grid_configure(row=2, column=0, sticky="nsew", pady=(22, 0))
            self.content.grid_columnconfigure(0, weight=3)
            self.content.grid_columnconfigure(1, weight=2)
            self.video_shell.grid_configure(row=0, column=0, sticky="nsew", padx=(0, 12))
            self.video_shell.grid_rowconfigure(0, weight=0)
            self.video_shell.grid_rowconfigure(1, weight=1)
            self.video_shell.configure(fg_color="#121625", corner_radius=24, border_width=1)
            self.video_title_label.grid(row=0, column=0, sticky="w", padx=22, pady=(22, 8))
            self.video_panel.grid_configure(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
            self.video_panel.grid_rowconfigure(0, weight=0)
            self.video_panel.grid_rowconfigure(1, weight=1)
            self.video_panel.configure(fg_color="#080a12", corner_radius=14, border_width=1)
            self.video_status_label.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 6))
            self.video_surface.grid_configure(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))
            self.video_surface.configure(corner_radius=8)
            self.video_hint_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
            self.pop_video_button.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
            self.stream_health_label.grid(row=2, column=0, sticky="ew", padx=22, pady=(0, 16))
            self.side_stack.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
            self.sidebar.grid(row=0, column=0, sticky="nsew")

        self.update_idletasks()

    def set_stream_health(self, state: str, detail: str = "") -> None:
        colors = {
            "Idle": "#7f8aa6",
            "Healthy": "#72f2c7",
            "Buffering": "#ffb86c",
            "Recovering": "#8cbcff",
            "Stopped": "#ff5f7a",
        }
        previous = getattr(self, "stream_health", "")
        self.stream_health = state
        message = f"Health: {state}"
        if detail:
            message = f"{message} - {detail}"
        if hasattr(self, "stream_health_label"):
            self.stream_health_label.configure(text=message, text_color=colors.get(state, "#7f8aa6"))
        if previous != state or detail:
            self.log(message)

    def _selected_stream(self) -> tuple[str, str, float] | None:
        url = sanitize_text(self.url_entry.get(), max_chars=300)
        quality = self.quality_option.get()
        volume = float(self.volume_slider.get())
        if not looks_like_url(url):
            messagebox.showerror("Invalid URL", "Please enter a full HTTPS Twitch URL, like https://www.twitch.tv/beardhero.")
            return None
        if quality != QUALITY_AUDIO_ONLY and quality not in LOW_VIDEO_QUALITIES:
            messagebox.showerror("Unsafe quality", "Choose a listed Twitch quality.")
            return None
        return url, quality, volume

    def start_stream(self, count_play: bool = True) -> bool:
        if self.is_streaming:
            self.log("A stream is already running.")
            return False
        if self.is_video_popped:
            self.stop_video_popout(user_requested=False)
        self.video_restart_attempts = 0

        selected = self._selected_stream()
        if selected is None:
            return False
        url, quality, volume = selected
        video_mode = self.playback_mode_option.get() == PLAYBACK_LOW_VIDEO
        allowed_qualities = LOW_VIDEO_QUALITIES if video_mode else (QUALITY_AUDIO_ONLY,)

        if quality not in allowed_qualities:
            messagebox.showerror("Unsafe quality", "Use audio_only in Audio mode, or choose a listed resolution in Video mode.")
            return False

        required_tools = ("streamlink", "mpv") if video_mode else ("streamlink", "ffplay")
        missing = [tool for tool in required_tools if shutil.which(tool) is None]
        if missing:
            messagebox.showerror(
                "Missing tools",
                "Install these command line tools first: " + ", ".join(missing),
            )
            self.log("Missing dependency: " + ", ".join(missing))
            return False

        try:
            if video_mode:
                self._start_embedded_video_pipe(url, quality, volume)
                self.video_status_label.configure(text=f"Playing in app: {channel_name_from_url(url)} at {quality}")
                self.set_stream_health("Healthy", f"{quality} buffer {VIDEO_CACHE_SECONDS}s")
            else:
                self.stream_process = subprocess.Popen(
                    self._streamlink_command(url, quality),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if self.stream_process.stdout is None:
                    raise RuntimeError("streamlink did not provide an audio pipe.")

                self.play_process = subprocess.Popen(
                    self._ffplay_command(volume),
                    stdin=self.stream_process.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.stream_process.stdout.close()
        except Exception as exc:
            self.stop_stream(user_requested=False)
            messagebox.showerror("Could not start stream", str(exc))
            self.log(f"Start failed: {exc}")
            return False

        self.history.save_launch(url, quality, volume, count_play=count_play, playback_mode=self.playback_mode_option.get())
        self.refresh_history(check_online=False)
        self.is_streaming = True
        self.status_label.configure(text="Streaming", text_color="#72f2c7")
        self.now_playing_label.configure(text=f"{channel_name_from_url(url)} using {quality}")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        if count_play:
            mode_label = "low-video" if video_mode else "audio"
            self.log(f"Started {mode_label} {quality} stream: {url}")
        else:
            self.log(f"Restarted audio pipe at {volume:.1f}x.")

        monitor = threading.Thread(target=self.monitor_stream, args=(url,), daemon=True)
        monitor.start()
        return True

    def _start_embedded_video_pipe(self, url: str, quality: str, volume: float) -> None:
        self.video_last_url = url
        self.video_last_quality = quality
        self.video_last_volume = volume
        self.stream_process = subprocess.Popen(
            self._streamlink_command(url, quality),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._start_stderr_drain(self.stream_process, "streamlink")
        if self.stream_process.stdout is None:
            raise RuntimeError("streamlink did not provide a video pipe.")
        self.embedded_video_process = subprocess.Popen(
            self._embedded_mpv_command(volume),
            stdin=self.stream_process.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._start_stderr_drain(self.embedded_video_process, "mpv")
        self.stream_process.stdout.close()

    def _cleanup_video_pipe(self) -> None:
        processes = [self.embedded_video_process, self.stream_process]
        for process in processes:
            if process and process.poll() is None:
                process.terminate()
        deadline = time.time() + 1.5
        for process in processes:
            if process and process.poll() is None:
                try:
                    process.wait(timeout=max(deadline - time.time(), 0.1))
                except subprocess.TimeoutExpired:
                    process.kill()
        self.embedded_video_process = None
        self.stream_process = None

    def toggle_video_popout(self) -> None:
        if self.is_video_popped:
            self.stop_video_popout(user_requested=True)
            return
        self.start_video_popout()

    def start_video_popout(self) -> bool:
        selected = self._selected_stream()
        if selected is None:
            return False
        url, quality, volume = selected
        if quality == QUALITY_AUDIO_ONLY:
            quality = "360p"
            self.quality_option.set(quality)
            self.playback_mode_option.set(PLAYBACK_LOW_VIDEO)
            self.on_playback_mode_changed(PLAYBACK_LOW_VIDEO)

        missing = [tool for tool in ("streamlink", "ffplay") if shutil.which(tool) is None]
        if missing:
            messagebox.showerror("Missing tools", "Install these command line tools first: " + ", ".join(missing))
            self.log("Missing dependency: " + ", ".join(missing))
            return False

        if self.is_streaming:
            self.stop_stream(user_requested=False)

        try:
            self.video_stream_process = subprocess.Popen(
                self._streamlink_command(url, quality),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if self.video_stream_process.stdout is None:
                raise RuntimeError("streamlink did not provide a video pipe.")
            self.video_play_process = subprocess.Popen(
                self._ffplay_video_command(volume),
                stdin=self.video_stream_process.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.video_stream_process.stdout.close()
        except Exception as exc:
            self.stop_video_popout(user_requested=False)
            messagebox.showerror("Could not start player window", str(exc))
            self.log(f"Player window failed: {exc}")
            return False

        self.history.save_launch(url, quality, volume, playback_mode=PLAYBACK_LOW_VIDEO)
        self.refresh_history(check_online=False)
        self.is_video_popped = True
        self.pop_video_button.configure(text="Stop Player Window")
        self.status_label.configure(text="Streaming", text_color="#72f2c7")
        self.now_playing_label.configure(text=f"{channel_name_from_url(url)} using {quality}")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.video_status_label.configure(text=f"Playing in player window: {channel_name_from_url(url)} at {quality}", text_color="#a7b0c8")
        self.set_stream_health("Healthy", "player window")
        self.log(f"Started player window {quality}: {url}")
        threading.Thread(target=self.monitor_video_popout, args=(url,), daemon=True).start()
        return True

    def stop_video_popout(self, user_requested: bool) -> None:
        processes = [self.video_play_process, self.video_stream_process]
        for process in processes:
            if process and process.poll() is None:
                process.terminate()
        deadline = time.time() + 2.5
        for process in processes:
            if process and process.poll() is None:
                try:
                    process.wait(timeout=max(deadline - time.time(), 0.1))
                except subprocess.TimeoutExpired:
                    process.kill()
        self.video_stream_process = None
        self.video_play_process = None
        self.is_video_popped = False
        self.pop_video_button.configure(text="Start Player Window")
        self.video_status_label.configure(text=VIDEO_READY_MESSAGE, text_color="#a7b0c8")
        self.set_stream_health("Idle" if user_requested else "Stopped")
        self.status_label.configure(text="Ready", text_color="#72f2c7")
        self.now_playing_label.configure(text="No active stream")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if user_requested:
            self.log("Stopped player window.")

    def monitor_video_popout(self, url: str) -> None:
        while self.is_video_popped:
            stream_code = self.video_stream_process.poll() if self.video_stream_process else None
            play_code = self.video_play_process.poll() if self.video_play_process else None
            if stream_code is not None or play_code is not None:
                self.event_queue.put(("video_popout_stopped", f"Player window ended: {channel_name_from_url(url)}"))
                return
            time.sleep(PROCESS_MONITOR_INTERVAL_SECONDS)

    def stop_stream(self, user_requested: bool) -> None:
        if self.is_video_popped:
            self.stop_video_popout(user_requested=user_requested)
            if not self.is_streaming:
                return
        self.stopping_stream = True
        self.cancel_volume_restart()
        if self.video_restart_after_id is not None:
            self.after_cancel(self.video_restart_after_id)
            self.video_restart_after_id = None
        processes = [self.play_process, self.stream_process, self.embedded_video_process]
        for process in processes:
            if process and process.poll() is None:
                process.terminate()

        deadline = time.time() + 2.5
        for process in processes:
            if process and process.poll() is None:
                remaining = max(deadline - time.time(), 0.1)
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    process.kill()

        self.stream_process = None
        self.play_process = None
        self.embedded_video_process = None
        self.is_streaming = False
        self.exit_embedded_fullscreen()
        self.video_status_label.configure(text=VIDEO_READY_MESSAGE, text_color="#a7b0c8")
        self.set_stream_health("Idle" if user_requested else "Stopped")
        self.status_label.configure(text="Ready", text_color="#72f2c7")
        self.now_playing_label.configure(text="No active stream")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if user_requested:
            self.log("Stopped stream.")
        self.stopping_stream = False

    def monitor_stream(self, url: str) -> None:
        started_at = time.time()
        last_heartbeat = 0.0
        while self.is_streaming:
            stream_code = self.stream_process.poll() if self.stream_process else None
            play_code = self.play_process.poll() if self.play_process else None
            embedded_code = self.embedded_video_process.poll() if self.embedded_video_process else None
            now = time.time()
            if now - last_heartbeat >= PROCESS_HEARTBEAT_SECONDS:
                last_heartbeat = now
                player_name = "mpv" if self.embedded_video_process is not None else "ffplay"
                if self.diagnostics_visible:
                    self.event_queue.put((
                        "diagnostic",
                        f"Pipeline alive {int(now - started_at)}s: streamlink={stream_code if stream_code is not None else 'running'}, {player_name}={embedded_code if self.embedded_video_process is not None and embedded_code is not None else play_code if play_code is not None else 'running'}",
                    ))
            if stream_code is not None or play_code is not None or embedded_code is not None:
                if self.stopping_stream:
                    return
                if self.embedded_video_process and embedded_code is None:
                    time.sleep(0.35)
                    embedded_code = self.embedded_video_process.poll()
                    if embedded_code is None:
                        self.embedded_video_process.terminate()
                        try:
                            self.embedded_video_process.wait(timeout=1.0)
                        except subprocess.TimeoutExpired:
                            self.embedded_video_process.kill()
                        embedded_code = self.embedded_video_process.poll()
                details = [f"Stream ended: {channel_name_from_url(url)}"]
                if stream_code is not None:
                    details.append(f"streamlink exit={stream_code}")
                    stream_error = self._process_error_tail(self.stream_process, "streamlink")
                    if stream_error:
                        details.append(f"streamlink: {stream_error}")
                if play_code is not None:
                    details.append(f"ffplay exit={play_code}")
                    play_error = self._process_error_tail(self.play_process, "ffplay")
                    if play_error:
                        details.append(f"ffplay: {play_error}")
                if embedded_code is not None:
                    details.append(f"mpv exit={embedded_code}")
                    mpv_error = self._process_error_tail(self.embedded_video_process, "mpv")
                    if mpv_error:
                        details.append(f"mpv: {mpv_error}")
                event = "video_retry" if self.embedded_video_process is not None or embedded_code is not None else "stopped"
                self.event_queue.put((event, "\n".join(details)))
                return
            time.sleep(PROCESS_MONITOR_INTERVAL_SECONDS)

    def retry_embedded_video(self, detail: str) -> None:
        if not self.is_streaming or self.stopping_stream:
            return
        if self.playback_mode_option.get() != PLAYBACK_LOW_VIDEO:
            self.stop_stream(user_requested=False)
            self.log(detail)
            return
        self.log(detail)
        if self.video_restart_attempts >= VIDEO_RETRY_LIMIT:
            self.video_status_label.configure(text="Video stopped after repeated buffering retries.", text_color="#ffb86c")
            self.set_stream_health("Stopped", "retry limit reached")
            self.stop_stream(user_requested=False)
            return
        self.video_restart_attempts += 1
        delay_ms = min(VIDEO_RETRY_BASE_MS + self.video_restart_attempts * 900, VIDEO_RETRY_MAX_MS)
        self.video_status_label.configure(
            text=f"Buffering video... reconnect {self.video_restart_attempts}/{VIDEO_RETRY_LIMIT}",
            text_color="#ffb86c",
        )
        self.set_stream_health("Buffering", f"reconnect {self.video_restart_attempts}/{VIDEO_RETRY_LIMIT}")
        self._cleanup_video_pipe()
        self.video_restart_after_id = self.after(delay_ms, self._restart_embedded_video)

    def _restart_embedded_video(self) -> None:
        self.video_restart_after_id = None
        if not self.is_streaming or self.stopping_stream:
            return
        try:
            self._start_embedded_video_pipe(self.video_last_url, self.video_last_quality, self.video_last_volume)
        except Exception as exc:
            self.event_queue.put(("video_retry", f"Video retry failed: {exc}"))
            return
        self.video_status_label.configure(
            text=f"Playing in app: {channel_name_from_url(self.video_last_url)} at {self.video_last_quality}",
            text_color="#a7b0c8",
        )
        self.set_stream_health("Recovering", "buffer refilled")
        self.after(5000, lambda: self.set_stream_health("Healthy", f"{self.video_last_quality} buffer {VIDEO_CACHE_SECONDS}s") if self.is_streaming and not self.stopping_stream else None)
        threading.Thread(target=self.monitor_stream, args=(self.video_last_url,), daemon=True).start()

    def process_events(self) -> None:
        processed = 0
        try:
            while processed < MAX_EVENTS_PER_TICK:
                event, detail = self.event_queue.get_nowait()
                processed += 1
                if event == "stopped" and self.is_streaming:
                    self.stop_stream(user_requested=False)
                    self.log(detail)
                    if "mpv exit" in detail or "streamlink exit" in detail:
                        self.video_status_label.configure(text=detail[:260], text_color="#ffb86c")
                elif event == "video_retry":
                    self.retry_embedded_video(detail)
                elif event == "video_popout_stopped" and self.is_video_popped:
                    self.stop_video_popout(user_requested=False)
                    self.log(detail)
                elif event == "diagnostic":
                    self.log(detail)
                elif event == "chat_message":
                    try:
                        payload = json.loads(detail)
                    except json.JSONDecodeError:
                        continue
                    user = sanitize_chat_user(payload.get("user"))
                    message = sanitize_chat_message(payload.get("message"))
                    if message:
                        channel = self.chat_channel or twitch_channel_from_url(self.url_entry.get())
                        if channel:
                            self.queue_chat_save(channel, user, message, "in")
                        self.add_chat_line(f"{user}: {message}")
                elif event == "chat_status":
                    color = "#72f2c7" if detail.startswith("Connected") else "#ffb86c"
                    self.set_chat_status(detail, color)
                elif event == "online_statuses":
                    self.online_refresh_in_progress = False
                    try:
                        decoded = json.loads(detail)
                    except json.JSONDecodeError:
                        continue
                    self.online_statuses = {str(key): bool(value) for key, value in decoded.items()}
                    if not self.is_streaming and not self.is_video_popped:
                        self.refresh_history(check_online=False)
                elif event == "online_error":
                    self.online_refresh_in_progress = False
                    self.log(detail)
        except queue.Empty:
            pass
        next_poll_ms = EVENT_POLL_ACTIVE_MS if processed else EVENT_POLL_IDLE_MS
        self.after(next_poll_ms, self.process_events)

    def load_record(self, record: StreamRecord) -> None:
        self.suppress_volume_restart = True
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, record.url)
            self.playback_mode_option.set(record.playback_mode)
            self.on_playback_mode_changed(self.playback_mode_option.get())
            self.quality_option.set(record.quality)
            self.volume_slider.set(record.volume)
            self._update_volume_label(record.volume)
        finally:
            self.suppress_volume_restart = False
        self.log(f"Loaded saved stream: {record.title}")

    def play_record(self, record: StreamRecord) -> None:
        self.load_record(record)
        self.start_stream()

    def delete_record(self, record: StreamRecord) -> None:
        if messagebox.askyesno("Delete saved stream", f"Delete {record.title} from encrypted history?"):
            self.history.delete_stream(record.id)
            self.refresh_history()
            self.log(f"Deleted saved stream: {record.title}")

    def clear_history(self) -> None:
        if messagebox.askyesno("Clear history", "Delete every saved stream from encrypted history?"):
            self.history.clear_history()
            self.refresh_history()
            self.log("Cleared saved stream history.")

    def change_history_password(self) -> None:
        dialog = PasswordChangeDialog(self)
        self.wait_window(dialog)
        if not dialog.result:
            return
        try:
            self.history.change_password(dialog.result)
        except Exception as exc:
            messagebox.showerror("Password change failed", str(exc))
            self.log(f"Password change failed: {exc}")
            return
        self.log("History vault password changed.")
        messagebox.showinfo("Password changed", "Saved streams were re-encrypted with the new password.")

    def on_close(self) -> None:
        if self.chat_flush_after_id is not None:
            self.after_cancel(self.chat_flush_after_id)
            self.chat_flush_after_id = None
        self.flush_chat_saves()
        self.disconnect_chat(user_requested=False)
        self.stop_stream(user_requested=False)
        self.history.close()
        self.destroy()


def unlock_history(root: ctk.CTk) -> EncryptedHistoryStore | None:
    while True:
        store = EncryptedHistoryStore(DB_PATH)
        dialog = PasswordDialog(root, first_run=store.is_new)
        root.wait_window(dialog)

        if not dialog.result:
            return None

        try:
            store.unlock(dialog.result)
            return store
        except ValueError as exc:
            messagebox.showerror("Unlock failed", str(exc))


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    gate = ctk.CTk()
    gate.withdraw()
    history = unlock_history(gate)
    gate.destroy()

    if history is None:
        return

    app = TwitchAudioApp(history)
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
