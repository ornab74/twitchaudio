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
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    import tkinter as tk
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
EXPECTED_LOGO_SHA256 = "d1f56736caa9f9cd80361aba9fb0bc8773df42653f4668a9d6a16752eb02bd86"
LOGO_FILENAMES = (f"{EXPECTED_LOGO_SHA256}.png", "logo.png")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_verified_logo_path() -> Path | None:
    base_dir = Path(__file__).resolve().parent
    candidates = [base_dir / filename for filename in LOGO_FILENAMES]
    for candidate in candidates:
        try:
            if candidate.is_file() and sha256_file(candidate) == EXPECTED_LOGO_SHA256:
                return candidate
        except OSError:
            continue
    return None
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
TWITCH_GQL_ENDPOINT = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_CHAT_SCOPES = "chat:read chat:edit"
PLAYBACK_AUDIO_ONLY = "Audio only"
PLAYBACK_LOW_VIDEO = "Video"
QUALITY_AUDIO_ONLY = "audio_only"
DEFAULT_VIDEO_QUALITY = "360p"
LOW_VIDEO_QUALITIES = (
    "160p",
    "160p30",
    "360p",
    "360p30",
    "480p",
    "480p30",
    "720p",
    "720p30",
    "720p60",
    "1080p",
    "1080p30",
    "1080p60",
    "1440p",
    "1440p30",
    "1440p60",
    "2160p",
    "2160p30",
    "2160p60",
    "best",
)
PLAYBACK_QUALITIES = (QUALITY_AUDIO_ONLY, *LOW_VIDEO_QUALITIES)
PLAYBACK_QUALITY_SET = frozenset(PLAYBACK_QUALITIES)
VIDEO_QUALITY_SET = frozenset(LOW_VIDEO_QUALITIES)
PLAYBACK_MODE_SET = frozenset((PLAYBACK_AUDIO_ONLY, PLAYBACK_LOW_VIDEO))
QUALITY_RE = re.compile(r"^(\d{3,4})p(?:(\d{2,3}))?$")
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
STREAMLINK_QUALITY_PROBE_TIMEOUT_SECONDS = 15.0
CHAT_SOCKET_TIMEOUT_SECONDS = 2.0
ONLINE_STATUS_REFRESH_SECONDS = 300
EXPLORE_CACHE_SECONDS = 120
EXPLORE_PAGE_SIZE = 25
EVENT_POLL_IDLE_MS = 2000
EVENT_POLL_ACTIVE_MS = 250
MAX_EVENTS_PER_TICK = 80
PROCESS_MONITOR_INTERVAL_SECONDS = 3.0
PROCESS_HEARTBEAT_SECONDS = 30.0
DIAGNOSTIC_LOG_INTERVAL_SECONDS = 1.5
VIDEO_READY_MESSAGE = "Video is ready. Choose Video mode and a resolution. FFplay is used for audio and video playback; double-click the in-app video for fullscreen."
TWITCH_CATEGORY_IDS = {
    "Software and Game Development": "1469308723",
    "Science & Technology": "509670",
    "Just Chatting": "509658",
    "Music": "26936",
    "Art": "509660",
    "Makers & Crafting": "509673",
    "Makers and Crafting": "509673",
    "Food & Drink": "509667",
    "Sports": "518203",
    "Talk Shows & Podcasts": "417752",
    "Special Events": "509663",
}
TWITCH_CATEGORY_SLUGS = {
    "Software & Game Development": "software-and-game-development",
    "Science & Technology": "science-and-technology",
    "Just Chatting": "just-chatting",
    "Music": "music",
    "Art": "art",
    "Makers & Crafting": "makers-and-crafting",
    "Makers and Crafting": "makers-and-crafting",
    "Food & Drink": "food-and-drink",
    "Sports": "sports",
    "Talk Shows & Podcasts": "talk-shows-and-podcasts",
    "Talk Shows and Podcasts": "talk-shows-and-podcasts",
    "Special Events": "special-events",
}
TWITCH_DIRECTORY_RESERVED_PATHS = {
    "about",
    "activate",
    "bits",
    "blog",
    "creatorcamp",
    "directory",
    "downloads",
    "jobs",
    "login",
    "p",
    "payments",
    "prime",
    "products",
    "settings",
    "signup",
    "store",
    "subscriptions",
    "team",
    "turbo",
    "videos",
}
SCIENCE_TECH_FALLBACK_QUERIES = (
    "Science & Technology",
    "Science and Technology",
    "science-and-technology",
    "science",
    "technology",
    "NASA",
    "space",
    "engineering",
    "programming",
)
FFPLAY_DOCK_TIMEOUT_SECONDS = 5.0
X11_CARDINAL_ATOM = 6
X11_KEY_PRESS = 2
X11_KEY_RELEASE = 3
X11_KEY_PRESS_MASK = 1
X11_KEY_RELEASE_MASK = 2
X11_BUTTON_PRESS = 4
X11_BUTTON_PRESS_MASK = 4
X11_CURRENT_TIME = 0
X11_REVERT_TO_PARENT = 2
_X11_LIBRARY: ctypes.CDLL | None = None
_X11_LIBRARY_CHECKED = False


class XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class XButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("button", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xbutton", XButtonEvent), ("pad", ctypes.c_long * 24)]


class XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]


X11_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(XErrorEvent))
_X11_ERROR_HANDLER_REF: Any | None = None


def _x11_ignore_error(_display: ctypes.c_void_p, _event: ctypes.POINTER(XErrorEvent)) -> int:
    return 0


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


def sanitize_playback_quality(value: Any, allow_audio: bool = True) -> str | None:
    quality = sanitize_text(value, max_chars=32)
    allowed = PLAYBACK_QUALITY_SET if allow_audio else VIDEO_QUALITY_SET
    if quality in allowed and (allow_audio or quality != QUALITY_AUDIO_ONLY):
        return quality
    return None


def sanitize_playback_mode(value: Any) -> str:
    mode = sanitize_text(value, max_chars=32)
    return mode if mode in PLAYBACK_MODE_SET else PLAYBACK_AUDIO_ONLY


def video_quality_profile(quality: str) -> tuple[int, int] | None:
    match = QUALITY_RE.fullmatch(quality)
    if not match:
        return None
    height = int(match.group(1))
    fps = int(match.group(2) or 30)
    return height, fps


def closest_video_quality(requested: str, available: list[str] | tuple[str, ...]) -> str | None:
    requested_quality = sanitize_playback_quality(requested, allow_audio=False)
    if requested_quality is None:
        return None

    safe_available = tuple(
        dict.fromkeys(
            quality
            for candidate in available
            if (quality := sanitize_playback_quality(candidate, allow_audio=False)) is not None
        )
    )
    if requested_quality in safe_available:
        return requested_quality
    if not safe_available:
        return None

    if requested_quality == "best":
        parseable = [(quality, video_quality_profile(quality)) for quality in safe_available]
        ranked = [(quality, profile) for quality, profile in parseable if profile is not None]
        if ranked:
            return max(ranked, key=lambda item: (item[1][0], item[1][1]))[0]
        return "best" if "best" in safe_available else safe_available[0]

    requested_profile = video_quality_profile(requested_quality)
    ranked = [
        (quality, profile)
        for quality in safe_available
        if (profile := video_quality_profile(quality)) is not None
    ]
    if requested_profile is not None and ranked:
        requested_height, requested_fps = requested_profile
        return min(
            ranked,
            key=lambda item: (
                abs(item[1][0] - requested_height),
                abs(item[1][1] - requested_fps),
                item[1][0] > requested_height,
                item[1][0],
            ),
        )[0]

    return "best" if "best" in safe_available else safe_available[0]


def _load_x11() -> ctypes.CDLL | None:
    global _X11_LIBRARY, _X11_LIBRARY_CHECKED
    if _X11_LIBRARY_CHECKED:
        return _X11_LIBRARY

    _X11_LIBRARY_CHECKED = True
    path = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        x11 = ctypes.CDLL(path)
    except OSError:
        return None

    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = ctypes.c_ulong
    x11.XGetWindowProperty.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_long,
        ctypes.c_long,
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
    ]
    x11.XGetWindowProperty.restype = ctypes.c_int
    x11.XQueryTree.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XQueryTree.restype = ctypes.c_int
    x11.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_char_p)]
    x11.XFetchName.restype = ctypes.c_int
    x11.XReparentWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
    x11.XReparentWindow.restype = ctypes.c_int
    x11.XMoveResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
    x11.XMoveResizeWindow.restype = ctypes.c_int
    x11.XSetWindowBorderWidth.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint]
    x11.XSetWindowBorderWidth.restype = ctypes.c_int
    x11.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XMapWindow.restype = ctypes.c_int
    x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XRaiseWindow.restype = ctypes.c_int
    x11.XDefaultScreen.argtypes = [ctypes.c_void_p]
    x11.XDefaultScreen.restype = ctypes.c_int
    x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDisplayWidth.restype = ctypes.c_int
    x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XDisplayHeight.restype = ctypes.c_int
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XFlush.restype = ctypes.c_int
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XFree.restype = ctypes.c_int
    x11.XSetErrorHandler.argtypes = [X11_ERROR_HANDLER]
    x11.XSetErrorHandler.restype = X11_ERROR_HANDLER
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XSync.restype = ctypes.c_int
    global _X11_ERROR_HANDLER_REF
    if _X11_ERROR_HANDLER_REF is None:
        _X11_ERROR_HANDLER_REF = X11_ERROR_HANDLER(_x11_ignore_error)
    x11.XSetErrorHandler(_X11_ERROR_HANDLER_REF)
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XKeysymToKeycode.restype = ctypes.c_uint
    x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    x11.XSetInputFocus.restype = ctypes.c_int
    x11.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p]
    x11.XSendEvent.restype = ctypes.c_int
    x11.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
    x11.XSelectInput.restype = ctypes.c_int
    x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(XEvent)]
    x11.XNextEvent.restype = ctypes.c_int
    x11.XQueryPointer.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XQueryPointer.restype = ctypes.c_int
    x11.XGetGeometry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XGetGeometry.restype = ctypes.c_int
    _X11_LIBRARY = x11
    return x11


def _x11_window_pid(x11: ctypes.CDLL, display: ctypes.c_void_p, window: int, pid_atom: int) -> int | None:
    if not window:
        return None
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    prop = ctypes.POINTER(ctypes.c_ubyte)()
    status = x11.XGetWindowProperty(
        display,
        ctypes.c_ulong(window),
        ctypes.c_ulong(pid_atom),
        0,
        1,
        0,
        X11_CARDINAL_ATOM,
        ctypes.byref(actual_type),
        ctypes.byref(actual_format),
        ctypes.byref(nitems),
        ctypes.byref(bytes_after),
        ctypes.byref(prop),
    )
    try:
        x11.XSync(display, 0)
    except Exception:
        pass
    if status != 0 or not prop:
        return None
    try:
        if nitems.value < 1 or actual_format.value != 32:
            return None
        return int(ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))[0])
    finally:
        x11.XFree(ctypes.cast(prop, ctypes.c_void_p))


def _x11_window_title(x11: ctypes.CDLL, display: ctypes.c_void_p, window: int) -> str:
    if not window:
        return ""
    name = ctypes.c_char_p()
    fetched = x11.XFetchName(display, ctypes.c_ulong(window), ctypes.byref(name))
    try:
        x11.XSync(display, 0)
    except Exception:
        pass
    if not fetched or not name.value:
        return ""
    try:
        return name.value.decode("utf-8", errors="replace")
    finally:
        x11.XFree(ctypes.cast(name, ctypes.c_void_p))


def _find_x11_window_for_ffplay(
    x11: ctypes.CDLL,
    display: ctypes.c_void_p,
    root: int,
    pid_atom: int,
    pid: int,
    title: str,
) -> int | None:
    stack = [root]
    while stack:
        window = stack.pop()
        try:
            matches_pid = bool(pid_atom) and _x11_window_pid(x11, display, window, pid_atom) == pid
            matches_title = bool(title) and _x11_window_title(x11, display, window) == title
        except Exception:
            continue
        if matches_pid or matches_title:
            return window

        root_return = ctypes.c_ulong()
        parent_return = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        nchildren = ctypes.c_uint()
        if not x11.XQueryTree(
            display,
            ctypes.c_ulong(window),
            ctypes.byref(root_return),
            ctypes.byref(parent_return),
            ctypes.byref(children),
            ctypes.byref(nchildren),
        ):
            continue
        try:
            if children:
                stack.extend(int(children[index]) for index in range(nchildren.value))
        finally:
            if children:
                x11.XFree(ctypes.cast(children, ctypes.c_void_p))
    return None


def dock_x11_window_for_pid(pid: int, parent_window_id: int, width: int, height: int, title: str = "") -> int | None:
    x11 = _load_x11()
    if x11 is None:
        return None
    display = x11.XOpenDisplay(None)
    if not display:
        return None
    try:
        root = int(x11.XDefaultRootWindow(display))
        pid_atom = int(x11.XInternAtom(display, b"_NET_WM_PID", 0))
        deadline = time.time() + FFPLAY_DOCK_TIMEOUT_SECONDS
        child_window: int | None = None
        while time.time() < deadline:
            child_window = _find_x11_window_for_ffplay(x11, display, root, pid_atom, pid, title)
            if child_window:
                break
            time.sleep(0.1)
        if not child_window:
            return None
        x11.XSetWindowBorderWidth(display, child_window, 0)
        x11.XReparentWindow(display, child_window, parent_window_id, 0, 0)
        x11.XMoveResizeWindow(display, child_window, 0, 0, max(width, 1), max(height, 1))
        x11.XMapWindow(display, child_window)
        x11.XFlush(display)
        return child_window
    finally:
        x11.XCloseDisplay(display)


def resize_x11_window(window_id: int, width: int, height: int) -> bool:
    x11 = _load_x11()
    if x11 is None:
        return False
    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        x11.XMoveResizeWindow(display, window_id, 0, 0, max(width, 1), max(height, 1))
        x11.XFlush(display)
        return True
    finally:
        x11.XCloseDisplay(display)


def reparent_x11_window(window_id: int, parent_window_id: int, width: int, height: int, x: int = 0, y: int = 0, raise_window: bool = False) -> bool:
    x11 = _load_x11()
    if x11 is None or not window_id or not parent_window_id:
        return False
    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        x11.XReparentWindow(display, ctypes.c_ulong(window_id), ctypes.c_ulong(parent_window_id), int(x), int(y))
        x11.XMoveResizeWindow(display, ctypes.c_ulong(window_id), int(x), int(y), max(int(width), 1), max(int(height), 1))
        x11.XMapWindow(display, ctypes.c_ulong(window_id))
        if raise_window:
            x11.XRaiseWindow(display, ctypes.c_ulong(window_id))
        x11.XFlush(display)
        try:
            x11.XSync(display, 0)
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        x11.XCloseDisplay(display)


def fullscreen_x11_window(window_id: int) -> bool:
    x11 = _load_x11()
    if x11 is None or not window_id:
        return False
    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        root = int(x11.XDefaultRootWindow(display))
        screen = int(x11.XDefaultScreen(display))
        width = int(x11.XDisplayWidth(display, screen))
        height = int(x11.XDisplayHeight(display, screen))
        if width <= 0 or height <= 0:
            return False
        x11.XSetWindowBorderWidth(display, ctypes.c_ulong(window_id), 0)
        x11.XReparentWindow(display, ctypes.c_ulong(window_id), ctypes.c_ulong(root), 0, 0)
        x11.XMoveResizeWindow(display, ctypes.c_ulong(window_id), 0, 0, width, height)
        x11.XMapWindow(display, ctypes.c_ulong(window_id))
        x11.XRaiseWindow(display, ctypes.c_ulong(window_id))
        x11.XFlush(display)
        try:
            x11.XSync(display, 0)
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        x11.XCloseDisplay(display)


def send_x11_key_to_window(window_id: int, key: str = "f") -> bool:
    x11 = _load_x11()
    if x11 is None:
        return False
    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        keysym = int(x11.XStringToKeysym(key.encode("ascii", errors="ignore")))
        if not keysym:
            return False
        keycode = int(x11.XKeysymToKeycode(display, ctypes.c_ulong(keysym)))
        if not keycode:
            return False
        root = int(x11.XDefaultRootWindow(display))
        x11.XSetInputFocus(display, ctypes.c_ulong(window_id), X11_REVERT_TO_PARENT, X11_CURRENT_TIME)
        for event_type in (X11_KEY_PRESS, X11_KEY_RELEASE):
            event = XKeyEvent()
            event.type = event_type
            event.display = display
            event.window = int(window_id)
            event.root = root
            event.subwindow = 0
            event.time = X11_CURRENT_TIME
            event.x = 1
            event.y = 1
            event.x_root = 1
            event.y_root = 1
            event.state = 0
            event.keycode = keycode
            event.same_screen = 1
            x11.XSendEvent(
                display,
                ctypes.c_ulong(window_id),
                1,
                X11_KEY_PRESS_MASK | X11_KEY_RELEASE_MASK,
                ctypes.byref(event),
            )
        x11.XFlush(display)
        return True
    finally:
        x11.XCloseDisplay(display)


def watch_x11_double_click(window_id: int, on_double_click: Callable[[], None]) -> bool:
    x11 = _load_x11()
    if x11 is None or not window_id:
        return False

    display = x11.XOpenDisplay(None)
    if not display:
        return False

    try:
        # FFplay is reparented over the Tk video surface, so Tk's double-click
        # binding will not reliably receive mouse events. Listen directly on the
        # FFplay X11 window instead of polling pointer state, which can miss fast
        # clicks.
        x11.XSelectInput(display, ctypes.c_ulong(window_id), X11_BUTTON_PRESS_MASK)
        x11.XFlush(display)

        last_click_at = 0.0
        last_button = 0
        while True:
            event = XEvent()
            x11.XNextEvent(display, ctypes.byref(event))
            if event.type != X11_BUTTON_PRESS:
                continue

            button = int(event.xbutton.button)
            now = time.time()
            if button == 1 and last_button == 1 and now - last_click_at <= 0.45:
                last_click_at = 0.0
                last_button = 0
                on_double_click()
            else:
                last_click_at = now
                last_button = button
    except Exception:
        return False
    finally:
        x11.XCloseDisplay(display)


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
            quality = sanitize_playback_quality(payload.get("quality")) or QUALITY_AUDIO_ONLY
            default_mode = PLAYBACK_LOW_VIDEO if quality in LOW_VIDEO_QUALITIES else PLAYBACK_AUDIO_ONLY
            playback_mode = sanitize_playback_mode(payload.get("playback_mode") or default_mode)
            if quality == QUALITY_AUDIO_ONLY:
                playback_mode = PLAYBACK_AUDIO_ONLY
            elif playback_mode == PLAYBACK_AUDIO_ONLY:
                playback_mode = PLAYBACK_LOW_VIDEO

            records.append(
                StreamRecord(
                    id=int(row["id"]),
                    title=str(payload.get("title") or channel_name_from_url(str(payload.get("url", "")))),
                    url=str(payload.get("url") or ""),
                    playback_mode=playback_mode,
                    quality=quality,
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
        normalized_url = sanitize_text(url, max_chars=300).strip()
        safe_quality = sanitize_playback_quality(quality)
        if safe_quality is None:
            raise ValueError("Unsupported playback quality.")
        quality = safe_quality
        default_mode = PLAYBACK_LOW_VIDEO if quality in LOW_VIDEO_QUALITIES else PLAYBACK_AUDIO_ONLY
        safe_playback_mode = sanitize_playback_mode(playback_mode or default_mode)
        if quality == QUALITY_AUDIO_ONLY:
            safe_playback_mode = PLAYBACK_AUDIO_ONLY
        elif safe_playback_mode == PLAYBACK_AUDIO_ONLY:
            safe_playback_mode = PLAYBACK_LOW_VIDEO
        title = channel_name_from_url(normalized_url)
        now = utc_now()

        for record in self.list_streams():
            if record.url == normalized_url:
                payload = {
                    "title": record.title or title,
                    "url": normalized_url,
                    "playback_mode": safe_playback_mode,
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
            "playback_mode": safe_playback_mode,
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

    def _category_query_aliases(self, category: str) -> list[str]:
        category = sanitize_text(category, max_chars=80)
        aliases = [category]

        def add_alias(value: str) -> None:
            alias = sanitize_text(value, max_chars=80)
            if alias and alias.lower() not in {item.lower() for item in aliases}:
                aliases.append(alias)

        add_alias(category.replace(" and ", " & "))
        add_alias(category.replace(" & ", " and "))
        add_alias(category.replace("-and-", " & ").replace("-", " ").title())
        slug = TWITCH_CATEGORY_SLUGS.get(category) or self._category_slug_from_name(category)
        add_alias(slug)
        add_alias(slug.replace("-", " ").title())
        if category.lower().replace("&", "and").replace("-", " ") in {"science and technology", "science and technology"} or slug == "science-and-technology":
            for alias in ("Science & Technology", "Science and Technology", "science-and-technology"):
                add_alias(alias)
        return aliases

    def _category_slug_from_name(self, category: str) -> str:
        slug = category.strip().lower().replace("&", "and")
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        return slug

    def _category_slug_candidates(self, category: str) -> list[str]:
        slugs: list[str] = []

        def add_slug(value: Any) -> None:
            slug = sanitize_text(value, max_chars=100).lower()
            slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
            if slug and slug not in slugs:
                slugs.append(slug)

        for alias in self._category_query_aliases(category):
            add_slug(TWITCH_CATEGORY_SLUGS.get(alias, ""))
            add_slug(alias if "-" in alias else self._category_slug_from_name(alias))
        return slugs

    def _fetch_twitch_directory_page(self, slug: str, max_bytes: int = 2 * 1024 * 1024) -> str:
        clean_slug = sanitize_text(slug, max_chars=100).lower()
        clean_slug = re.sub(r"[^a-z0-9-]+", "-", clean_slug).strip("-")
        if not clean_slug:
            return ""
        url = f"https://www.twitch.tv/directory/category/{urllib.parse.quote(clean_slug)}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 TwitchFreedom/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.twitch.tv/",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(max_bytes)
        except Exception:
            return ""
        return html.unescape(raw.decode("utf-8", errors="replace"))

    def _category_id_from_directory_page(self, slug: str, category_aliases: list[str]) -> str:
        page = self._fetch_twitch_directory_page(slug, max_bytes=3 * 1024 * 1024)
        if not page:
            return ""

        clean_slug = sanitize_text(slug, max_chars=100).lower()
        clean_slug = re.sub(r"[^a-z0-9-]+", "-", clean_slug).strip("-")
        normalized_aliases = {
            sanitize_text(alias, max_chars=100).lower().replace("&", "and").replace("-", " ")
            for alias in category_aliases
            if sanitize_text(alias, max_chars=100)
        }
        normalized_aliases.add(clean_slug.replace("-", " "))

        def snippet_matches_category(snippet: str) -> bool:
            normalized_snippet = snippet.lower().replace("&", "and").replace("-", " ")
            return (clean_slug and clean_slug in snippet.lower()) or any(
                alias and alias in normalized_snippet for alias in normalized_aliases
            )

        patterns = (
            r'"id"\s*:\s*"(\d+)".{0,1600}"slug"\s*:\s*"' + re.escape(clean_slug) + r'"',
            r'"slug"\s*:\s*"' + re.escape(clean_slug) + r'".{0,1600}"id"\s*:\s*"(\d+)"',
            r'"game"\s*:\s*\{.{0,1200}"id"\s*:\s*"(\d+)"',
            r'"targetID"\s*:\s*"(\d+)".{0,600}"targetType"\s*:\s*"GAME"',
            r'"gameID"\s*:\s*"(\d+)"',
            r'"gameId"\s*:\s*"(\d+)"',
            r'"categoryID"\s*:\s*"(\d+)"',
            r'"categoryId"\s*:\s*"(\d+)"',
        )
        for source in (page, page.replace(r"\u0026", "&").replace(r"\/", "/")):
            for pattern in patterns:
                for match in re.finditer(pattern, source, flags=re.DOTALL):
                    start = max(0, match.start() - 900)
                    end = min(len(source), match.end() + 900)
                    if snippet_matches_category(source[start:end]):
                        return match.group(1)
        return ""

    def _category_id_candidates(self, category: str) -> list[str]:
        category = sanitize_text(category, max_chars=80)
        aliases = self._category_query_aliases(category)
        candidates: list[str] = []

        def add_candidate(value: Any) -> None:
            candidate = str(value or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)

        for slug in self._category_slug_candidates(category):
            add_candidate(self._category_id_from_directory_page(slug, aliases))

        for alias in aliases:
            add_candidate(TWITCH_CATEGORY_IDS.get(alias, ""))

        for alias in aliases:
            games = self.helix_get("/games", [("name", alias)])
            for item in games.get("data", []):
                if not isinstance(item, dict):
                    continue
                name = sanitize_text(item.get("name"), max_chars=80)
                if name.lower() == alias.lower() or name.lower() == category.lower():
                    add_candidate(item.get("id"))

        for alias in aliases:
            search = self.helix_get("/search/categories", [("query", alias), ("first", "20")])
            for item in search.get("data", []):
                if not isinstance(item, dict):
                    continue
                name = sanitize_text(item.get("name"), max_chars=80)
                if name.lower() in {candidate.lower() for candidate in aliases}:
                    add_candidate(item.get("id"))
            if not candidates:
                for item in search.get("data", [])[:3]:
                    if isinstance(item, dict):
                        add_candidate(item.get("id"))
        return candidates

    def _stream_record_from_parts(self, login: Any, display_name: Any, title: Any) -> dict[str, str] | None:
        clean_login = sanitize_chat_user(login)
        if clean_login == "unknown":
            return None
        clean_display_name = sanitize_text(display_name, max_chars=80) or clean_login
        return {
            "login": clean_login,
            "display_name": clean_display_name,
            "title": sanitize_chat_message(title),
            "url": f"https://www.twitch.tv/{clean_login}",
        }

    def _dedupe_streams(self, streams: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for stream in streams:
            login = stream.get("login", "")
            if not login or login in seen:
                continue
            seen.add(login)
            unique.append(stream)
            if len(unique) >= max(1, min(limit, 100)):
                break
        return unique

    def _streams_for_category_id_page(self, category_id: str, limit: int, cursor: str | None = None) -> tuple[list[dict[str, str]], str]:
        params = [("game_id", category_id), ("first", str(max(1, min(limit, 100))))]
        if cursor:
            params.append(("after", cursor))
        payload = self.helix_get("/streams", params)
        streams: list[dict[str, str]] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            record = self._stream_record_from_parts(item.get("user_login"), item.get("user_name"), item.get("title"))
            if record:
                streams.append(record)
        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
        next_cursor = str(pagination.get("cursor") or "")
        return streams, next_cursor

    def _streams_for_category_id(self, category_id: str, limit: int) -> list[dict[str, str]]:
        streams, _next_cursor = self._streams_for_category_id_page(category_id, limit)
        return streams

    def _streams_from_top_live_matching_page(
        self,
        category_aliases: list[str],
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, str]], str]:
        params = [("first", "100")]
        if cursor:
            params.append(("after", cursor))
        payload = self.helix_get("/streams", params)
        normalized_aliases = {
            sanitize_text(alias, max_chars=100).lower().replace("&", "and").replace("-", " ")
            for alias in category_aliases
            if sanitize_text(alias, max_chars=100)
        }
        streams: list[dict[str, str]] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            game_name = sanitize_text(item.get("game_name"), max_chars=100).lower().replace("&", "and").replace("-", " ")
            if game_name not in normalized_aliases:
                continue
            record = self._stream_record_from_parts(item.get("user_login"), item.get("user_name"), item.get("title"))
            if record:
                streams.append(record)
                if len(streams) >= max(1, min(limit, 100)):
                    break
        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
        next_cursor = str(pagination.get("cursor") or "")
        return self._dedupe_streams(streams, limit), next_cursor

    def _search_live_channels(self, queries: list[str], limit: int, category_aliases: list[str]) -> list[dict[str, str]]:
        exact_matches: list[dict[str, str]] = []
        loose_matches: list[dict[str, str]] = []
        normalized_aliases = {alias.lower().replace("&", "and") for alias in category_aliases}
        for query in queries:
            search_query = sanitize_text(query, max_chars=80)
            if not search_query:
                continue
            payload = self.helix_get(
                "/search/channels",
                [("query", search_query), ("live_only", "true"), ("first", "100")],
            )
            for item in payload.get("data", []):
                if not isinstance(item, dict) or item.get("is_live") is False:
                    continue
                record = self._stream_record_from_parts(
                    item.get("broadcaster_login"),
                    item.get("display_name"),
                    item.get("title"),
                )
                if not record:
                    continue
                game_name = sanitize_text(item.get("game_name"), max_chars=80).lower().replace("&", "and")
                if game_name and game_name in normalized_aliases:
                    exact_matches.append(record)
                else:
                    loose_matches.append(record)
        return self._dedupe_streams([*exact_matches, *loose_matches], limit)

    def _streams_for_channel_logins(self, logins: list[str], limit: int) -> list[dict[str, str]]:
        clean_logins = []
        for login in logins:
            clean_login = sanitize_chat_user(login.lower())
            if clean_login != "unknown" and clean_login not in clean_logins:
                clean_logins.append(clean_login)
        streams: list[dict[str, str]] = []
        for index in range(0, min(len(clean_logins), 100), 100):
            params = [("user_login", login) for login in clean_logins[index : index + 100]]
            if not params:
                continue
            payload = self.helix_get("/streams", params)
            for item in payload.get("data", []):
                if not isinstance(item, dict):
                    continue
                record = self._stream_record_from_parts(item.get("user_login"), item.get("user_name"), item.get("title"))
                if record:
                    streams.append(record)
        return self._dedupe_streams(streams, limit)

    def _streams_from_twitch_graphql_category(self, category: str, slug: str, limit: int) -> list[dict[str, str]]:
        clean_category = sanitize_text(category, max_chars=80)
        clean_slug = sanitize_text(slug, max_chars=100).lower()
        clean_slug = re.sub(r"[^a-z0-9-]+", "-", clean_slug).strip("-")
        gql_queries: list[dict[str, Any]] = []

        # Twitch's web directory is slug-driven, while Helix is category-id driven.
        # Some categories can come back empty through Helix in app tokens, so this
        # fallback asks the same public GraphQL endpoint used by Twitch's directory.
        if clean_slug:
            gql_queries.append({
                "operationName": "TwitchAudioDirectoryBySlug",
                "variables": {"slug": clean_slug, "limit": max(1, min(limit, 100))},
                "query": """
                query TwitchAudioDirectoryBySlug($slug: String!, $limit: Int!) {
                  game(slug: $slug) {
                    streams(first: $limit, options: {sort: VIEWER_COUNT}) {
                      edges { node { title broadcaster { login displayName } } }
                    }
                  }
                }
                """,
            })
        if clean_category:
            gql_queries.append({
                "operationName": "TwitchAudioDirectoryByName",
                "variables": {"name": clean_category, "limit": max(1, min(limit, 100))},
                "query": """
                query TwitchAudioDirectoryByName($name: String!, $limit: Int!) {
                  game(name: $name) {
                    streams(first: $limit, options: {sort: VIEWER_COUNT}) {
                      edges { node { title broadcaster { login displayName } } }
                    }
                  }
                }
                """,
            })

        streams: list[dict[str, str]] = []
        for payload in gql_queries:
            request = urllib.request.Request(
                TWITCH_GQL_ENDPOINT,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Client-Id": TWITCH_WEB_CLIENT_ID,
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 TwitchAudio/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    raw = response.read(512 * 1024)
                decoded = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            if not isinstance(decoded, dict):
                continue
            data = decoded.get("data")
            game = data.get("game") if isinstance(data, dict) else None
            stream_connection = game.get("streams") if isinstance(game, dict) else None
            edges = stream_connection.get("edges", []) if isinstance(stream_connection, dict) else []
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                broadcaster = node.get("broadcaster") if isinstance(node, dict) else None
                if not isinstance(broadcaster, dict):
                    continue
                record = self._stream_record_from_parts(
                    broadcaster.get("login"),
                    broadcaster.get("displayName"),
                    node.get("title") if isinstance(node, dict) else "",
                )
                if record:
                    streams.append(record)
            streams = self._dedupe_streams(streams, limit)
            if streams:
                return streams
        return self._dedupe_streams(streams, limit)

    def _streams_from_directory_slug(self, slug: str, limit: int) -> list[dict[str, str]]:
        slug = sanitize_text(slug, max_chars=100).lower()
        slug = re.sub(r"[^a-z0-9-]+", "-", slug).strip("-")
        if not slug:
            return []
        page = self._fetch_twitch_directory_page(slug)
        if not page:
            return []
        candidates: list[str] = []

        def add_login(value: str) -> None:
            login = sanitize_chat_user(value.lower())
            if login == "unknown" or login in TWITCH_DIRECTORY_RESERVED_PATHS or login in candidates:
                return
            candidates.append(login)

        for pattern in (
            r'"broadcaster_login"\s*:\s*"([A-Za-z0-9_]{3,25})"',
            r'"user_login"\s*:\s*"([A-Za-z0-9_]{3,25})"',
            r'"login"\s*:\s*"([A-Za-z0-9_]{3,25})"',
            r'href="/([A-Za-z0-9_]{3,25})(?:[/?#"]|$)',
        ):
            for match in re.finditer(pattern, page):
                add_login(match.group(1))
                if len(candidates) >= max(limit * 3, 30):
                    break
            if len(candidates) >= max(limit * 3, 30):
                break

        streams = self._streams_for_channel_logins(candidates, limit)
        if streams:
            return streams
        fallback_streams: list[dict[str, str]] = []
        for login in candidates[:limit]:
            record = self._stream_record_from_parts(login, login, f"Live in {slug.replace('-', ' ').title()}")
            if record:
                fallback_streams.append(record)
        return self._dedupe_streams(fallback_streams, limit)

    def _decode_explore_cursor(self, cursor: str | None) -> tuple[str, str, str]:
        if not cursor:
            return "", "", ""
        parts = str(cursor).split(":", 2)
        if len(parts) != 3:
            return "", "", ""
        return parts[0], parts[1], parts[2]

    def get_category_streams_page(self, category: str, limit: int = 30, cursor: str | None = None) -> dict[str, Any]:
        clean_category = sanitize_text(category, max_chars=80)
        category_aliases = self._category_query_aliases(clean_category)
        cursor_kind, cursor_key, cursor_value = self._decode_explore_cursor(cursor)

        if cursor_kind == "helix" and cursor_key:
            streams, next_cursor = self._streams_for_category_id_page(cursor_key, limit, cursor_value)
            return {
                "streams": self._dedupe_streams(streams, limit),
                "next_cursor": f"helix:{cursor_key}:{next_cursor}" if next_cursor else "",
                "source": f"Helix category {cursor_key}",
            }

        if cursor_kind == "toplive":
            streams, next_cursor = self._streams_from_top_live_matching_page(category_aliases, limit, cursor_value)
            return {
                "streams": streams,
                "next_cursor": f"toplive:all:{next_cursor}" if next_cursor else "",
                "source": "top live category filter",
            }

        category_ids = self._category_id_candidates(clean_category)
        for category_id in category_ids:
            streams, next_cursor = self._streams_for_category_id_page(category_id, limit)
            streams = self._dedupe_streams(streams, limit)
            if streams:
                return {
                    "streams": streams,
                    "next_cursor": f"helix:{category_id}:{next_cursor}" if next_cursor else "",
                    "source": f"Helix category {category_id}",
                }

        streams, next_cursor = self._streams_from_top_live_matching_page(category_aliases, limit)
        if streams:
            return {
                "streams": streams,
                "next_cursor": f"toplive:all:{next_cursor}" if next_cursor else "",
                "source": "top live category filter",
            }

        for slug in self._category_slug_candidates(clean_category):
            streams = self._streams_from_twitch_graphql_category(clean_category, slug, limit)
            if streams:
                return {"streams": streams, "next_cursor": "", "source": f"Twitch directory GraphQL {slug}"}

        for slug in self._category_slug_candidates(clean_category):
            streams = self._streams_from_directory_slug(slug, limit)
            if streams:
                return {"streams": streams, "next_cursor": "", "source": f"Twitch directory page {slug}"}

        fallback_queries = list(category_aliases)
        normalized_category = clean_category.lower().replace("&", "and").replace("-", " ")
        if normalized_category == "science and technology" or "science-and-technology" in category_aliases:
            fallback_queries.extend(SCIENCE_TECH_FALLBACK_QUERIES)
        streams = self._search_live_channels(fallback_queries, limit, category_aliases)
        return {"streams": streams, "next_cursor": "", "source": "live channel search"}

    def get_category_streams(self, category: str, limit: int = 30) -> list[dict[str, str]]:
        page = self.get_category_streams_page(category, limit)
        streams = page.get("streams", [])
        return streams if isinstance(streams, list) else []

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
        self.page_index = 0
        self.loaded_pages: list[dict[str, Any]] = []
        self.loading_page = False
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
        content.grid_rowconfigure(3, weight=0)
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
        self.stream_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 8))

        self.page_nav = ctk.CTkFrame(content, fg_color="transparent")
        self.page_nav.grid(row=3, column=0, sticky="ew", padx=18, pady=(0, 12))
        self.prev_page_button = ctk.CTkButton(
            self.page_nav,
            text="Previous",
            width=96,
            fg_color="#26304a",
            hover_color="#34405f",
            state="disabled",
            command=self._previous_page,
        )
        self.prev_page_button.pack(side="left")
        self.page_label = ctk.CTkLabel(
            self.page_nav,
            text="Page 1",
            text_color="#8b96b3",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.page_label.pack(side="left", padx=14)
        self.next_page_button = ctk.CTkButton(
            self.page_nav,
            text="Next",
            width=96,
            fg_color="#26304a",
            hover_color="#34405f",
            state="disabled",
            command=self._next_page,
        )
        self.next_page_button.pack(side="left")

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
        self.page_index = 0
        self.loaded_pages = []
        self.loading_page = False
        for name, button in self.category_buttons.items():
            button.configure(
                fg_color="#151a2a" if name == category else "transparent",
                text_color="#f6f8ff" if name == category else "#a7b0c8",
            )
        self.category_title.configure(text=category)
        self.category_subtitle.configure(text="Live now, sorted by Twitch. No viewer counts shown.")
        self._set_stream_message("Loading live streams...")
        self._update_page_controls()
        threading.Thread(target=self._load_category_page, args=(category, 0, None), daemon=True).start()

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

    def _load_category_page(self, category: str, page_index: int, cursor: str | None) -> None:
        self.loading_page = True
        try:
            result = self.oauth.get_category_streams_page(category, limit=EXPLORE_PAGE_SIZE, cursor=cursor)
        except Exception as exc:
            self.after(0, lambda: self._set_stream_message(f"Could not load streams: {exc}"))
            self.after(0, self._update_page_controls)
            self.loading_page = False
            return
        self.after(0, lambda: self._store_and_render_page(category, page_index, result))

    def _store_and_render_page(self, category: str, page_index: int, result: dict[str, Any]) -> None:
        self.loading_page = False
        if category != self.active_category:
            return
        while len(self.loaded_pages) <= page_index:
            self.loaded_pages.append({"streams": [], "next_cursor": "", "source": ""})
        self.loaded_pages[page_index] = result
        self.page_index = page_index
        streams = result.get("streams", [])
        self._render_category(category, streams if isinstance(streams, list) else [])

    def _current_page(self) -> dict[str, Any]:
        if 0 <= self.page_index < len(self.loaded_pages):
            return self.loaded_pages[self.page_index]
        return {"streams": [], "next_cursor": "", "source": ""}

    def _next_page(self) -> None:
        if self.loading_page:
            return
        next_index = self.page_index + 1
        if next_index < len(self.loaded_pages):
            page = self.loaded_pages[next_index]
            self.page_index = next_index
            streams = page.get("streams", [])
            self._render_category(self.active_category, streams if isinstance(streams, list) else [])
            return
        next_cursor = str(self._current_page().get("next_cursor") or "")
        if not next_cursor:
            return
        self._set_stream_message("Loading next page...")
        self._update_page_controls()
        threading.Thread(target=self._load_category_page, args=(self.active_category, next_index, next_cursor), daemon=True).start()

    def _previous_page(self) -> None:
        if self.loading_page or self.page_index <= 0:
            return
        self.page_index -= 1
        page = self._current_page()
        streams = page.get("streams", [])
        self._render_category(self.active_category, streams if isinstance(streams, list) else [])

    def _update_page_controls(self) -> None:
        current = self._current_page()
        next_cursor = str(current.get("next_cursor") or "")
        self.page_label.configure(text=f"Page {self.page_index + 1}")
        self.prev_page_button.configure(state="normal" if self.page_index > 0 and not self.loading_page else "disabled")
        has_loaded_next = self.page_index + 1 < len(self.loaded_pages)
        can_go_next = bool(next_cursor) or has_loaded_next
        self.next_page_button.configure(state="normal" if can_go_next and not self.loading_page else "disabled")

    def _render_category(self, category: str, streams: list[dict[str, str]]) -> None:
        if category != self.active_category:
            return
        for child in self.stream_frame.winfo_children():
            child.destroy()
        page = self._current_page()
        source = str(page.get("source") or "Twitch")
        self.category_subtitle.configure(text=f"Live now, sorted by Twitch. Page {self.page_index + 1}. Source: {source}.")
        self._update_page_controls()
        if not streams:
            self._set_stream_message(
                "No live streams found for this category. Try clicking the category again to bypass any old cache; "
                "if it still happens, Twitch returned an empty category/API result for this account or region."
            )
            self._update_page_controls()
            return
        start_rank = self.page_index * EXPLORE_PAGE_SIZE
        for index, stream in enumerate(streams, start=start_rank + 1):
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
        self.docked_video_window_id: int | None = None
        self.ffplay_video_fullscreen = False
        self.ffplay_dock_parent_id: int | None = None
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
        self.logo_label: tk.Label | None = None
        logo_path = find_verified_logo_path()
        if logo_path is not None:
            try:
                logo_image = PhotoImage(file=str(logo_path))
                width = max(1, logo_image.width())
                height = max(1, logo_image.height())
                shrink = max(1, int(max((width + 234) // 235, (height + 133) // 134)))
                if shrink > 1:
                    logo_image = logo_image.subsample(shrink, shrink)
                self.logo_image = logo_image
                self.logo_label = tk.Label(
                    sidebar,
                    image=self.logo_image,
                    text="",
                    bg="#0d111f",
                    bd=0,
                    highlightthickness=0,
                    padx=0,
                    pady=0,
                )
                self.logo_label.pack(anchor="w", padx=24, pady=(20, 4))
                self.log(f"Verified logo loaded: sha256={EXPECTED_LOGO_SHA256}")
            except Exception as exc:
                self.log(f"Verified logo could not be loaded: {exc}")
        else:
            self.log("Logo skipped: no matching sha256-verified logo file found.")

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
        self.video_surface.bind("<Double-Button-1>", self.toggle_ffplay_fullscreen)
        self.video_surface.bind("<Configure>", self.resize_docked_video)
        video_placeholder = ctk.CTkLabel(
            self.video_surface,
            text="Double-click video for fullscreen",
            text_color="#6f7a92",
        )
        video_placeholder.pack(expand=True)
        video_placeholder.bind("<Double-Button-1>", self.toggle_ffplay_fullscreen)
        self.video_hint_label = ctk.CTkLabel(
            video_panel,
            text="Start Video uses FFplay by default. On X11 it docks into this panel; double-click the video to toggle FFplay fullscreen.",
            font=ctk.CTkFont(size=12),
            text_color="#6f7a92",
            wraplength=640,
            justify="left",
        )
        self.video_hint_label.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self.ffplay_fullscreen_button = ctk.CTkButton(
            video_panel,
            text="Fullscreen",
            height=32,
            command=self.toggle_ffplay_fullscreen,
        )
        self.ffplay_fullscreen_button.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 12))
        # The separate FFplay Dock button was removed. Video starts from the main Start Video button.
        # Fullscreen is controlled by double-clicking the video area or pressing this button.
        self.pop_video_button = None
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

    def _streamlink_available_qualities(self, url: str) -> tuple[str, ...]:
        command = [
            "streamlink",
            "--json",
            "--twitch-disable-ads",
            url,
        ]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=STREAMLINK_QUALITY_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Timed out checking available Twitch resolutions.") from exc

        if result.returncode != 0:
            detail = sanitize_text(result.stderr or result.stdout, max_chars=300)
            message = "Streamlink could not list available Twitch resolutions."
            if detail:
                message = f"{message} {detail}"
            raise RuntimeError(message)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            detail = sanitize_text(result.stdout or result.stderr, max_chars=300)
            message = "Streamlink returned an unreadable resolution list."
            if detail:
                message = f"{message} {detail}"
            raise RuntimeError(message) from exc

        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, dict):
            return ()

        qualities: list[str] = []
        for name in streams:
            quality = sanitize_playback_quality(name)
            if quality and quality not in qualities:
                qualities.append(quality)
        return tuple(qualities)

    def _resolve_video_quality(self, url: str, requested: str) -> str:
        requested_quality = sanitize_playback_quality(requested, allow_audio=False)
        if requested_quality is None:
            raise ValueError("Choose a listed Twitch resolution.")

        available = self._streamlink_available_qualities(url)
        resolved = closest_video_quality(requested_quality, available)
        if resolved is None:
            raise RuntimeError("This stream did not expose any supported video resolutions.")

        if resolved != requested_quality:
            available_video = [quality for quality in available if quality in LOW_VIDEO_QUALITIES]
            offered = ", ".join(available_video[:8])
            if len(available_video) > 8:
                offered = f"{offered}, ..."
            self.quality_option.set(resolved)
            self.log(
                f"{requested_quality} is unavailable for this stream; using closest supported quality {resolved}"
                + (f" from: {offered}." if offered else ".")
            )
        return resolved

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

    def _ffplay_video_command(self, volume: float, window_title: str) -> list[str]:
        return [
            "ffplay",
            "-autoexit",
            "-window_title",
            window_title,
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

    def _embedded_ffplay_command(self, volume: float, window_title: str) -> list[str]:
        return self._ffplay_video_command(volume, window_title)

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

    def resize_docked_video(self, _event: object | None = None) -> None:
        if self.docked_video_window_id is None or self.ffplay_video_fullscreen:
            return
        resize_x11_window(
            self.docked_video_window_id,
            self.video_surface.winfo_width(),
            self.video_surface.winfo_height(),
        )

    def _dock_ffplay_player(
        self,
        process: subprocess.Popen[bytes],
        parent_window_id: int,
        width: int,
        height: int,
        window_title: str,
    ) -> None:
        window_id = dock_x11_window_for_pid(process.pid, parent_window_id, width, height, window_title)

        def finish() -> None:
            if self.video_play_process is not process or not self.is_video_popped:
                return
            if window_id is None:
                self.video_status_label.configure(
                    text="FFplay started in its own window. X11 docking was unavailable.",
                    text_color="#ffb86c",
                )
                self.log("FFplay docking unavailable; using external player window.")
                return
            self.docked_video_window_id = window_id
            self.ffplay_dock_parent_id = parent_window_id
            self.ffplay_video_fullscreen = False
            self._start_ffplay_double_click_watcher(window_id)
            self.resize_docked_video()
            self.video_status_label.configure(text="FFplay docked in app. Double-click the video area or press Fullscreen to toggle FFplay fullscreen.", text_color="#a7b0c8")
            self.log("Docked ffplay into the video panel.")

        self.after(0, finish)

    def _dock_embedded_ffplay_player(
        self,
        process: subprocess.Popen[bytes],
        parent_window_id: int,
        width: int,
        height: int,
        window_title: str,
    ) -> None:
        window_id = dock_x11_window_for_pid(process.pid, parent_window_id, width, height, window_title)

        def finish() -> None:
            if self.embedded_video_process is not process or not self.is_streaming:
                return
            if window_id is None:
                self.video_status_label.configure(
                    text="FFplay started in its own window. X11 docking was unavailable.",
                    text_color="#ffb86c",
                )
                self.log("FFplay docking unavailable; using external player window.")
                return
            self.docked_video_window_id = window_id
            self.ffplay_dock_parent_id = parent_window_id
            self.ffplay_video_fullscreen = False
            self._start_ffplay_double_click_watcher(window_id)
            self.resize_docked_video()
            self.video_status_label.configure(text="FFplay docked in app. Double-click the video area or press Fullscreen to toggle FFplay fullscreen.", text_color="#a7b0c8")
            self.log("Docked ffplay into the video panel.")

        self.after(0, finish)

    def _start_ffplay_double_click_watcher(self, window_id: int) -> None:
        def on_double_click() -> None:
            self.after(0, self.toggle_ffplay_fullscreen)

        threading.Thread(
            target=watch_x11_double_click,
            args=(window_id, on_double_click),
            daemon=True,
        ).start()


    def toggle_ffplay_fullscreen(self, _event: object | None = None) -> None:
        if self.docked_video_window_id is None:
            if not self.is_streaming and not self.is_video_popped:
                self.video_status_label.configure(
                    text="Start Video first, then double-click the video to toggle FFplay fullscreen.",
                    text_color="#ffb86c",
                )
                self.log("FFplay fullscreen requested before video was started.")
                return
            self.video_status_label.configure(
                text="FFplay is still attaching. Once the video appears, double-click the video area again.",
                text_color="#ffb86c",
            )
            self.log("FFplay fullscreen request ignored because no docked FFplay window is available yet.")
            return

        if self.ffplay_video_fullscreen:
            dock_parent = int(self.video_surface.winfo_id())
            self.ffplay_dock_parent_id = dock_parent
            if reparent_x11_window(
                self.docked_video_window_id,
                dock_parent,
                self.video_surface.winfo_width(),
                self.video_surface.winfo_height(),
            ):
                self.ffplay_video_fullscreen = False
                self.video_status_label.configure(text="FFplay returned to the app. Double-click the video to fullscreen.", text_color="#a7b0c8")
                self.log("Returned FFplay window from fullscreen to the dock.")
                return
            self.video_status_label.configure(text="Could not return FFplay to the dock. Press F in the FFplay window or restart video.", text_color="#ffb86c")
            self.log("Could not re-dock FFplay window from fullscreen.")
            return

        if fullscreen_x11_window(self.docked_video_window_id):
            self.ffplay_video_fullscreen = True
            self.video_status_label.configure(text="FFplay video is fullscreen. Double-click the video or press Fullscreen again to return it to the app.", text_color="#a7b0c8")
            self.log("Moved FFplay video window to fullscreen.")
            return

        if send_x11_key_to_window(self.docked_video_window_id, "f"):
            self.video_status_label.configure(text="Sent fullscreen toggle to FFplay. Double-click again to exit.", text_color="#a7b0c8")
            self.log("Sent fullscreen toggle to FFplay window.")
            return
        self.video_status_label.configure(
            text="Could not toggle FFplay fullscreen. Click the FFplay video and press F.",
            text_color="#ffb86c",
        )
        self.log("Could not toggle FFplay fullscreen.")


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
            if self.pop_video_button is not None:
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
            if self.pop_video_button is not None:
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
        quality = sanitize_playback_quality(self.quality_option.get())
        volume = float(self.volume_slider.get())
        if not looks_like_url(url):
            messagebox.showerror("Invalid URL", "Please enter a full HTTPS Twitch URL, like https://www.twitch.tv/beardhero.")
            return None
        if quality is None:
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

        required_tools = ("streamlink", "ffplay")
        missing = [tool for tool in required_tools if shutil.which(tool) is None]
        if missing:
            messagebox.showerror(
                "Missing tools",
                "Install these command line tools first: " + ", ".join(missing),
            )
            self.log("Missing dependency: " + ", ".join(missing))
            return False

        if video_mode:
            try:
                quality = self._resolve_video_quality(url, quality)
            except Exception as exc:
                messagebox.showerror("Could not choose video quality", str(exc))
                self.log(f"Quality fallback failed: {exc}")
                return False

        try:
            if video_mode:
                self._start_embedded_video_pipe(url, quality, volume)
                self.video_status_label.configure(text=f"Starting FFplay: {channel_name_from_url(url)} at {quality}")
                self.set_stream_health("Healthy", "ffplay")
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
        self.update_idletasks()
        parent_window_id = int(self.video_surface.winfo_id())
        dock_width = max(self.video_surface.winfo_width(), 1)
        dock_height = max(self.video_surface.winfo_height(), 1)
        window_title = f"TwitchAudio FFplay {os.getpid()} {int(time.time() * 1000)}"
        self.embedded_video_process = subprocess.Popen(
            self._embedded_ffplay_command(volume, window_title),
            stdin=self.stream_process.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._start_stderr_drain(self.embedded_video_process, "ffplay-video")
        self.stream_process.stdout.close()
        threading.Thread(
            target=self._dock_embedded_ffplay_player,
            args=(self.embedded_video_process, parent_window_id, dock_width, dock_height, window_title),
            daemon=True,
        ).start()

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
        self.docked_video_window_id = None
        self.ffplay_video_fullscreen = False
        self.ffplay_dock_parent_id = None

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
            quality = DEFAULT_VIDEO_QUALITY
            self.quality_option.set(quality)
            self.playback_mode_option.set(PLAYBACK_LOW_VIDEO)
            self.on_playback_mode_changed(PLAYBACK_LOW_VIDEO)

        missing = [tool for tool in ("streamlink", "ffplay") if shutil.which(tool) is None]
        if missing:
            messagebox.showerror("Missing tools", "Install these command line tools first: " + ", ".join(missing))
            self.log("Missing dependency: " + ", ".join(missing))
            return False

        try:
            quality = self._resolve_video_quality(url, quality)
        except Exception as exc:
            messagebox.showerror("Could not choose video quality", str(exc))
            self.log(f"Quality fallback failed: {exc}")
            return False

        if self.is_streaming:
            self.stop_stream(user_requested=False)

        self.update_idletasks()
        parent_window_id = int(self.video_surface.winfo_id())
        dock_width = max(self.video_surface.winfo_width(), 1)
        dock_height = max(self.video_surface.winfo_height(), 1)
        window_title = f"TwitchFreedom FFplay {os.getpid()} {int(time.time() * 1000)}"

        try:
            self.video_stream_process = subprocess.Popen(
                self._streamlink_command(url, quality),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if self.video_stream_process.stdout is None:
                raise RuntimeError("streamlink did not provide a video pipe.")
            self.video_play_process = subprocess.Popen(
                self._ffplay_video_command(volume, window_title),
                stdin=self.video_stream_process.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.video_stream_process.stdout.close()
        except Exception as exc:
            self.stop_video_popout(user_requested=False)
            messagebox.showerror("Could not start FFplay Dock", str(exc))
            self.log(f"FFplay Dock failed: {exc}")
            return False

        self.history.save_launch(url, quality, volume, playback_mode=PLAYBACK_LOW_VIDEO)
        self.refresh_history(check_online=False)
        self.is_video_popped = True
        if self.pop_video_button is not None:
            self.pop_video_button.configure(text="Stop FFplay Dock")
        self.status_label.configure(text="Streaming", text_color="#72f2c7")
        self.now_playing_label.configure(text=f"{channel_name_from_url(url)} using {quality}")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.video_status_label.configure(text=f"Starting FFplay video: {channel_name_from_url(url)} at {quality}", text_color="#a7b0c8")
        self.set_stream_health("Healthy", "ffplay")
        self.log(f"Started ffplay video {quality}: {url}")
        threading.Thread(
            target=self._dock_ffplay_player,
            args=(self.video_play_process, parent_window_id, dock_width, dock_height, window_title),
            daemon=True,
        ).start()
        threading.Thread(target=self.monitor_video_popout, args=(url,), daemon=True).start()
        return True

    def stop_video_popout(self, user_requested: bool) -> None:
        self.docked_video_window_id = None
        self.ffplay_video_fullscreen = False
        self.ffplay_dock_parent_id = None
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
        if self.pop_video_button is not None:
            self.pop_video_button.configure(text="Start FFplay Dock")
        self.video_status_label.configure(text=VIDEO_READY_MESSAGE, text_color="#a7b0c8")
        self.set_stream_health("Idle" if user_requested else "Stopped")
        self.status_label.configure(text="Ready", text_color="#72f2c7")
        self.now_playing_label.configure(text="No active stream")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if user_requested:
            self.log("Stopped ffplay video.")

    def monitor_video_popout(self, url: str) -> None:
        while self.is_video_popped:
            stream_code = self.video_stream_process.poll() if self.video_stream_process else None
            play_code = self.video_play_process.poll() if self.video_play_process else None
            if stream_code is not None or play_code is not None:
                self.event_queue.put(("video_popout_stopped", f"FFplay ended: {channel_name_from_url(url)}"))
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
                player_name = "ffplay"
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
                    details.append(f"ffplay-video exit={embedded_code}")
                    ffplay_video_error = self._process_error_tail(self.embedded_video_process, "ffplay-video")
                    if ffplay_video_error:
                        details.append(f"ffplay-video: {ffplay_video_error}")
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
        self.set_stream_health("Recovering", "ffplay reconnect")
        self.after(5000, lambda: self.set_stream_health("Healthy", "ffplay") if self.is_streaming and not self.stopping_stream else None)
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
                    if "ffplay-video exit" in detail or "streamlink exit" in detail:
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
