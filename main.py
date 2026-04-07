import base64
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from tkinter import messagebox

    import customtkinter as ctk
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
except ModuleNotFoundError as exc:
    missing = exc.name or "a required package"
    print(f"Missing Python dependency: {missing}")
    print("Install dependencies with: python3 -m pip install -r requirements.txt")
    sys.exit(1)


APP_NAME = "TwitchAudio"
DEFAULT_STREAM_URL = "https://www.twitch.tv/beardhero"
APP_DIR = Path.home() / ".twitchaudio"
DB_PATH = APP_DIR / "history.sqlite3"
KDF_ITERATIONS = 600_000
SALT_BYTES = 16
VERIFY_TEXT = b"twitchaudio-history-v1"
MAX_HISTORY = 80


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
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def channel_name_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.netloc and "twitch.tv" in parsed.netloc.lower():
        channel = parsed.path.strip("/").split("/")[0]
        if channel:
            return channel
    return parsed.netloc or url.strip()


def looks_like_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


@dataclass
class StreamRecord:
    id: int
    title: str
    url: str
    quality: str
    volume: float
    created_at: str
    updated_at: str
    last_played_at: str | None
    play_count: int


class EncryptedHistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None
        self.fernet: Fernet | None = None

    @property
    def is_new(self) -> bool:
        return not self.path.exists()

    def unlock(self, password: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._ensure_schema()

        salt_value = self._get_meta("salt")
        if not salt_value:
            salt = os.urandom(SALT_BYTES)
            self.fernet = Fernet(derive_key(password, salt))
            self._set_meta("salt", base64.urlsafe_b64encode(salt).decode("ascii"))
            self._set_meta("verifier", self.fernet.encrypt(VERIFY_TEXT).decode("ascii"))
            self._set_meta("created_at", utc_now())
            self.connection.commit()
            return

        try:
            salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
            self.fernet = Fernet(derive_key(password, salt))
            verifier = self._get_meta("verifier") or ""
            if self.fernet.decrypt(verifier.encode("ascii")) != VERIFY_TEXT:
                raise InvalidToken
        except (InvalidToken, ValueError, TypeError) as exc:
            self.close()
            raise ValueError("That password could not unlock this history vault.") from exc

    def close(self) -> None:
        if self.connection:
            self.connection.close()
        self.connection = None
        self.fernet = None

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
        if not self.fernet:
            raise RuntimeError("History vault is locked.")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.fernet.encrypt(raw)

    def _decrypt_payload(self, payload: bytes) -> dict[str, Any]:
        if not self.fernet:
            raise RuntimeError("History vault is locked.")
        raw = self.fernet.decrypt(payload)
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Encrypted payload was not a JSON object.")
        return decoded

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
            except (InvalidToken, ValueError, json.JSONDecodeError):
                continue

            records.append(
                StreamRecord(
                    id=int(row["id"]),
                    title=str(payload.get("title") or channel_name_from_url(str(payload.get("url", "")))),
                    url=str(payload.get("url") or ""),
                    quality=str(payload.get("quality") or "audio_only"),
                    volume=float(payload.get("volume") or 2.0),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                    last_played_at=row["last_played_at"],
                    play_count=int(row["play_count"] or 0),
                )
            )

        return records

    def save_launch(self, url: str, quality: str, volume: float, count_play: bool = True) -> StreamRecord:
        assert self.connection is not None
        normalized_url = url.strip()
        title = channel_name_from_url(normalized_url)
        now = utc_now()

        for record in self.list_streams():
            if record.url == normalized_url:
                payload = {
                    "title": record.title or title,
                    "url": normalized_url,
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

    def change_password(self, new_password: str) -> None:
        assert self.connection is not None
        records = self.list_streams()
        salt = os.urandom(SALT_BYTES)
        self.fernet = Fernet(derive_key(new_password, salt))
        self._set_meta("salt", base64.urlsafe_b64encode(salt).decode("ascii"))
        self._set_meta("verifier", self.fernet.encrypt(VERIFY_TEXT).decode("ascii"))

        for record in records:
            payload = {
                "title": record.title,
                "url": record.url,
                "quality": record.quality,
                "volume": record.volume,
            }
            self.connection.execute(
                "UPDATE streams SET payload = ?, updated_at = ? WHERE id = ?",
                (self._encrypt_payload(payload), utc_now(), record.id),
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


class PasswordDialog(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, first_run: bool) -> None:
        super().__init__(master)
        self.result: str | None = None
        self.first_run = first_run

        self.title("Unlock TwitchAudio")
        self.geometry("440x360" if first_run else "440x300")
        self.resizable(False, False)
        self.configure(fg_color="#090b13")
        self.grab_set()
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


class StreamCard(ctk.CTkFrame):
    def __init__(self, master: ctk.CTkFrame, record: StreamRecord, app: "TwitchAudioApp") -> None:
        super().__init__(master, fg_color="#151a2a", corner_radius=18, border_width=1, border_color="#26334f")
        self.record = record
        self.app = app

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 0))

        ctk.CTkLabel(
            header,
            text=record.title,
            font=ctk.CTkFont(size=17, weight="bold"),
            text_color="#f6f8ff",
        ).pack(side="left", anchor="w")
        ctk.CTkLabel(
            header,
            text=f"{record.play_count} plays",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#72f2c7",
        ).pack(side="right", anchor="e")

        ctk.CTkLabel(
            self,
            text=record.url,
            font=ctk.CTkFont(size=12),
            text_color="#9aa6bf",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(3, 0))
        ctk.CTkLabel(
            self,
            text=f"Last played: {display_time(record.last_played_at)}  |  Quality: {record.quality}  |  Volume: {record.volume:.1f}x",
            font=ctk.CTkFont(size=12),
            text_color="#6f7a92",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(3, 12))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(actions, text="Play", width=76, command=lambda: app.play_record(record)).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(
            actions,
            text="Load",
            width=76,
            fg_color="#26304a",
            hover_color="#34405f",
            command=lambda: app.load_record(record),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Delete",
            width=76,
            fg_color="#3b1d2a",
            hover_color="#5a263b",
            command=lambda: app.delete_record(record),
        ).pack(side="right")


class TwitchAudioApp(ctk.CTk):
    def __init__(self, history: EncryptedHistoryStore) -> None:
        super().__init__()
        self.history = history
        self.stream_process: subprocess.Popen[bytes] | None = None
        self.play_process: subprocess.Popen[bytes] | None = None
        self.event_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.volume_restart_after_id: str | None = None
        self.suppress_volume_restart = False
        self.is_streaming = False

        self.title("TwitchAudio Command Deck")
        self.geometry("1180x760")
        self.minsize(980, 650)
        self.configure(fg_color="#080a12")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self.refresh_history()
        self.log("Encrypted history vault unlocked.")
        self.after(300, self.process_events)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=285, fg_color="#0d111f", corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        ctk.CTkLabel(
            sidebar,
            text="TwitchAudio",
            font=ctk.CTkFont(size=32, weight="bold"),
            text_color="#f6f8ff",
        ).pack(anchor="w", padx=26, pady=(32, 2))
        ctk.CTkLabel(
            sidebar,
            text="Audio-only command deck",
            font=ctk.CTkFont(size=13),
            text_color="#7f8aa6",
        ).pack(anchor="w", padx=28)

        self.status_card = ctk.CTkFrame(sidebar, fg_color="#151a2a", corner_radius=22, border_width=1, border_color="#26334f")
        self.status_card.pack(fill="x", padx=22, pady=(32, 18))

        self.status_label = ctk.CTkLabel(
            self.status_card,
            text="Ready",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#72f2c7",
        )
        self.status_label.pack(anchor="w", padx=20, pady=(18, 4))
        self.now_playing_label = ctk.CTkLabel(
            self.status_card,
            text="No active stream",
            font=ctk.CTkFont(size=13),
            text_color="#a7b0c8",
            wraplength=220,
            justify="left",
        )
        self.now_playing_label.pack(anchor="w", padx=20, pady=(0, 18))

        metric_card = ctk.CTkFrame(sidebar, fg_color="#10192d", corner_radius=22, border_width=1, border_color="#214163")
        metric_card.pack(fill="x", padx=22, pady=(0, 18))
        ctk.CTkLabel(
            metric_card,
            text="Low Bandwidth Mode",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#8cbcff",
        ).pack(anchor="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(
            metric_card,
            text="Audio-only usually uses about 60-80 MB/hour instead of multiple GB/hour for 1080p video.",
            font=ctk.CTkFont(size=12),
            text_color="#a7b0c8",
            wraplength=220,
            justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 18))

        ctk.CTkButton(sidebar, text="Use BeardHero", command=self.use_default_stream).pack(fill="x", padx=22, pady=(10, 8))
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

        ctk.CTkLabel(
            sidebar,
            text=f"Vault: {DB_PATH}",
            font=ctk.CTkFont(size=11),
            text_color="#5f6980",
            wraplength=235,
            justify="left",
        ).pack(side="bottom", anchor="w", padx=24, pady=24)

        main = ctk.CTkFrame(self, fg_color="#080a12", corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew", padx=26, pady=26)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        hero = ctk.CTkFrame(main, fg_color="#121625", corner_radius=24, border_width=1, border_color="#24304a")
        hero.grid(row=0, column=0, sticky="ew")
        hero.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hero,
            text="Live Stream Control",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(22, 0))
        ctk.CTkLabel(
            hero,
            text="Pipe Twitch audio_only through streamlink into ffplay without pulling video.",
            font=ctk.CTkFont(size=13),
            text_color="#8b96b3",
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(4, 18))

        controls = ctk.CTkFrame(hero, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 22))
        controls.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(controls, height=48, placeholder_text="https://www.twitch.tv/channel")
        self.url_entry.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 14))
        self.url_entry.insert(0, DEFAULT_STREAM_URL)

        self.quality_option = ctk.CTkOptionMenu(controls, values=["audio_only"], height=40)
        self.quality_option.set("audio_only")
        self.quality_option.grid(row=1, column=0, sticky="w")

        self.volume_slider = ctk.CTkSlider(controls, from_=0.5, to=3.0, number_of_steps=25, command=self._update_volume_label)
        self.volume_slider.grid(row=1, column=1, sticky="ew", padx=16)
        self.volume_label = ctk.CTkLabel(controls, text="Volume 2.0x", width=100, text_color="#a7b0c8")
        self.volume_label.grid(row=1, column=2, sticky="e")
        self.volume_slider.set(2.0)
        self._update_volume_label(2.0)

        actions = ctk.CTkFrame(hero, fg_color="transparent")
        actions.grid(row=2, column=1, sticky="e", padx=24, pady=(0, 22))
        self.start_button = ctk.CTkButton(actions, text="Start Audio", width=150, height=48, command=self.start_stream)
        self.start_button.pack(side="left", padx=(0, 12))
        self.stop_button = ctk.CTkButton(
            actions,
            text="Stop",
            width=110,
            height=48,
            fg_color="#3b1d2a",
            hover_color="#5a263b",
            state="disabled",
            command=lambda: self.stop_stream(user_requested=True),
        )
        self.stop_button.pack(side="left")

        content = ctk.CTkFrame(main, fg_color="transparent")
        content.grid(row=2, column=0, sticky="nsew", pady=(22, 0))
        content.grid_columnconfigure(0, weight=3)
        content.grid_columnconfigure(1, weight=2)
        content.grid_rowconfigure(0, weight=1)

        history_shell = ctk.CTkFrame(content, fg_color="#121625", corner_radius=24, border_width=1, border_color="#24304a")
        history_shell.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        history_shell.grid_rowconfigure(1, weight=1)
        history_shell.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            history_shell,
            text="Encrypted Stream History",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(22, 12))

        self.history_frame = ctk.CTkScrollableFrame(history_shell, fg_color="transparent")
        self.history_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

        log_shell = ctk.CTkFrame(content, fg_color="#121625", corner_radius=24, border_width=1, border_color="#24304a")
        log_shell.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        log_shell.grid_rowconfigure(1, weight=1)
        log_shell.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_shell,
            text="Diagnostics",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#f6f8ff",
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(22, 12))

        self.log_box = ctk.CTkTextbox(log_shell, fg_color="#080a12", text_color="#dbe3ff", border_width=0, wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.log_box.configure(state="disabled")

    def use_default_stream(self) -> None:
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, DEFAULT_STREAM_URL)
        self.log("Loaded BeardHero preset.")

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
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def refresh_history(self) -> None:
        for child in self.history_frame.winfo_children():
            child.destroy()

        records = self.history.list_streams()
        if not records:
            empty = ctk.CTkFrame(self.history_frame, fg_color="#151a2a", corner_radius=18, border_width=1, border_color="#26334f")
            empty.pack(fill="x", padx=2, pady=4)
            ctk.CTkLabel(
                empty,
                text="No saved streams yet",
                font=ctk.CTkFont(size=17, weight="bold"),
                text_color="#f6f8ff",
            ).pack(anchor="w", padx=18, pady=(18, 4))
            ctk.CTkLabel(
                empty,
                text="Start a stream and it will be saved here encrypted with your vault password.",
                font=ctk.CTkFont(size=13),
                text_color="#9aa6bf",
                wraplength=460,
                justify="left",
            ).pack(anchor="w", padx=18, pady=(0, 18))
            return

        for record in records:
            card = StreamCard(self.history_frame, record, self)
            card.pack(fill="x", padx=2, pady=6)

    def start_stream(self, count_play: bool = True) -> bool:
        if self.is_streaming:
            self.log("A stream is already running.")
            return False

        url = self.url_entry.get().strip()
        quality = self.quality_option.get()
        volume = float(self.volume_slider.get())

        if not looks_like_url(url):
            messagebox.showerror("Invalid URL", "Please enter a full Twitch URL, like https://www.twitch.tv/beardhero.")
            return False

        missing = [tool for tool in ("streamlink", "ffplay") if shutil.which(tool) is None]
        if missing:
            messagebox.showerror(
                "Missing tools",
                "Install these command line tools first: " + ", ".join(missing),
            )
            self.log("Missing dependency: " + ", ".join(missing))
            return False

        stream_command = [
            "streamlink",
            "--loglevel",
            "none",
            "--stdout",
            "--twitch-disable-ads",
            "--stream-segment-threads",
            "2",
            url,
            quality,
        ]
        play_command = [
            "ffplay",
            "-nodisp",
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

        try:
            self.stream_process = subprocess.Popen(
                stream_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if self.stream_process.stdout is None:
                raise RuntimeError("streamlink did not provide an audio pipe.")

            self.play_process = subprocess.Popen(
                play_command,
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

        self.history.save_launch(url, quality, volume, count_play=count_play)
        self.refresh_history()
        self.is_streaming = True
        self.status_label.configure(text="Streaming", text_color="#72f2c7")
        self.now_playing_label.configure(text=f"{channel_name_from_url(url)} using {quality}")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        if count_play:
            self.log(f"Started {quality} stream: {url}")
        else:
            self.log(f"Restarted audio pipe at {volume:.1f}x.")

        monitor = threading.Thread(target=self.monitor_stream, args=(url,), daemon=True)
        monitor.start()
        return True

    def stop_stream(self, user_requested: bool) -> None:
        self.cancel_volume_restart()
        processes = [self.play_process, self.stream_process]
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
        self.is_streaming = False
        self.status_label.configure(text="Ready", text_color="#72f2c7")
        self.now_playing_label.configure(text="No active stream")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if user_requested:
            self.log("Stopped stream.")

    def monitor_stream(self, url: str) -> None:
        while self.is_streaming:
            stream_code = self.stream_process.poll() if self.stream_process else None
            play_code = self.play_process.poll() if self.play_process else None
            if stream_code is not None or play_code is not None:
                self.event_queue.put(("stopped", f"Stream ended: {channel_name_from_url(url)}"))
                return
            time.sleep(1)

    def process_events(self) -> None:
        try:
            while True:
                event, detail = self.event_queue.get_nowait()
                if event == "stopped" and self.is_streaming:
                    self.stop_stream(user_requested=False)
                    self.log(detail)
        except queue.Empty:
            pass
        self.after(300, self.process_events)

    def load_record(self, record: StreamRecord) -> None:
        self.suppress_volume_restart = True
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, record.url)
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
