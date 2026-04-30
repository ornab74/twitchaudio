#!/usr/bin/env bash
set -euo pipefail

APP_NAME="TwitchFreedom"
REPO_OWNER="${TWITCHFREEDOM_REPO_OWNER:-ornab74}"
REPO_NAME="${TWITCHFREEDOM_REPO_NAME:-twitchfreedom}"
REPO_REF="${TWITCHFREEDOM_REPO_REF:-main}"
INSTALL_DIR="${TWITCHFREEDOM_INSTALL_DIR:-$HOME/.local/opt/twitchfreedom}"
BIN_DIR="${TWITCHFREEDOM_BIN_DIR:-$HOME/.local/bin}"
CREATE_DESKTOP="${TWITCHFREEDOM_CREATE_DESKTOP:-1}"
RUN_AFTER_INSTALL="${TWITCHFREEDOM_RUN_AFTER_INSTALL:-1}"
SOURCE_DIR="${TWITCHFREEDOM_SOURCE_DIR:-}"

log() {
    printf '[%s] %s\n' "$APP_NAME" "$*"
}

die() {
    printf '[%s] ERROR: %s\n' "$APP_NAME" "$*" >&2
    exit 1
}

run_privileged() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "Missing system packages and sudo is not available. Install Python 3, python3-venv, Tk, FFmpeg, curl, and tar manually."
    fi
}

install_linux_packages() {
    local needs_packages=0
    command -v python3 >/dev/null 2>&1 || needs_packages=1
    command -v ffplay >/dev/null 2>&1 || needs_packages=1
    command -v tar >/dev/null 2>&1 || needs_packages=1
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        needs_packages=1
    fi
    if command -v python3 >/dev/null 2>&1 && ! python3 -c "import tkinter" >/dev/null 2>&1; then
        needs_packages=1
    fi

    if [ "$needs_packages" = "0" ]; then
        return
    fi

    if command -v apt-get >/dev/null 2>&1; then
        log "Installing Debian/Ubuntu system packages"
        run_privileged apt-get update
        run_privileged apt-get install -y ca-certificates curl ffmpeg python3 python3-tk python3-venv tar
    elif command -v dnf >/dev/null 2>&1; then
        log "Installing Fedora system packages"
        run_privileged dnf install -y ca-certificates curl ffmpeg python3 python3-tkinter tar
    elif command -v pacman >/dev/null 2>&1; then
        log "Installing Arch system packages"
        run_privileged pacman -Sy --needed --noconfirm ca-certificates curl ffmpeg python python-tk tar
    elif command -v zypper >/dev/null 2>&1; then
        log "Installing openSUSE system packages"
        run_privileged zypper install -y ca-certificates curl ffmpeg python3 python3-tk tar
    else
        die "Unsupported package manager. Install Python 3, venv, Tk, FFmpeg, curl or wget, and tar, then rerun this script."
    fi
}

download_file() {
    local url="$1"
    local output="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$output"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$output" "$url"
    else
        die "curl or wget is required to download Twitch Freedom."
    fi
}

find_local_source() {
    if [ -n "$SOURCE_DIR" ]; then
        printf '%s\n' "$SOURCE_DIR"
        return
    fi

    local script_path="${BASH_SOURCE[0]:-}"
    if [ -n "$script_path" ] && [ -f "$script_path" ]; then
        local script_dir
        script_dir="$(cd "$(dirname "$script_path")" && pwd)"
        if [ -f "$script_dir/../main.py" ] && [ -f "$script_dir/../requirements.txt" ]; then
            cd "$script_dir/.." && pwd
            return
        fi
    fi
}

copy_source_tree() {
    local src="$1"
    mkdir -p "$INSTALL_DIR"
    (
        cd "$src"
        tar \
            --exclude='.git' \
            --exclude='.venv' \
            --exclude='venv' \
            --exclude='env' \
            --exclude='__pycache__' \
            --exclude='.pytest_cache' \
            --exclude='.mypy_cache' \
            --exclude='.ruff_cache' \
            --exclude='.codex' \
            --exclude='*.sqlite' \
            --exclude='*.sqlite3' \
            --exclude='*.db' \
            -cf - .
    ) | (
        cd "$INSTALL_DIR"
        tar -xf -
    )
}

install_source() {
    local local_source
    local_source="$(find_local_source || true)"

    if [ -n "$local_source" ]; then
        log "Installing from local checkout: $local_source"
        copy_source_tree "$local_source"
        return
    fi

    local tmp_dir archive extracted
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT
    archive="$tmp_dir/source.tar.gz"
    log "Downloading https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/$REPO_REF.tar.gz"
    download_file "https://github.com/$REPO_OWNER/$REPO_NAME/archive/refs/heads/$REPO_REF.tar.gz" "$archive"
    tar -xzf "$archive" -C "$tmp_dir"
    extracted="$(find "$tmp_dir" -maxdepth 1 -type d -name "$REPO_NAME-*" | head -n 1)"
    [ -n "$extracted" ] || die "Could not find extracted source directory."
    copy_source_tree "$extracted"
}

python_bin() {
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
    elif command -v python >/dev/null 2>&1; then
        command -v python
    else
        die "Python 3 is required."
    fi
}

install_python_deps() {
    local py venv_python req_file
    py="$(python_bin)"
    req_file="$INSTALL_DIR/requirements.txt"
    [ -f "$req_file" ] || die "Missing requirements.txt in $INSTALL_DIR"

    if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
        log "Creating virtual environment"
        "$py" -m venv "$INSTALL_DIR/.venv"
    fi

    venv_python="$INSTALL_DIR/.venv/bin/python"
    log "Installing Python requirements"
    "$venv_python" -m pip install --upgrade pip
    "$venv_python" -m pip install --upgrade -r "$req_file"
}

write_launcher() {
    local runner="$INSTALL_DIR/twitchfreedom.sh"
    mkdir -p "$BIN_DIR"
    cat > "$runner" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/main.py" "\$@"
EOF
    chmod +x "$runner"
    ln -sf "$runner" "$BIN_DIR/twitchfreedom"
    log "Command launcher: $BIN_DIR/twitchfreedom"
}

write_desktop_entry() {
    [ "$CREATE_DESKTOP" = "1" ] || return

    local app_dir icon_dir desktop_file icon_source icon_target
    app_dir="$HOME/.local/share/applications"
    icon_dir="$HOME/.local/share/icons/hicolor/256x256/apps"
    desktop_file="$app_dir/twitchfreedom.desktop"
    icon_source="$INSTALL_DIR/logo.png"
    icon_target="$icon_dir/twitchfreedom.png"

    mkdir -p "$app_dir" "$icon_dir"
    if [ -f "$icon_source" ]; then
        cp "$icon_source" "$icon_target"
    fi

    cat > "$desktop_file" <<EOF
[Desktop Entry]
Name=TwitchFreedom
Comment=Minimal Twitch GUI without browser bloat
GenericName=Twitch Player
Exec=$INSTALL_DIR/twitchfreedom.sh
Icon=twitchfreedom
Type=Application
Terminal=false
StartupNotify=false
StartupWMClass=TwitchFreedom
Categories=AudioVideo;Player;
Keywords=twitch;stream;audio;privacy;
EOF
    chmod +x "$desktop_file"
    update-desktop-database "$app_dir" >/dev/null 2>&1 || true
    gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
    log "Desktop launcher: $desktop_file"
}

main() {
    install_linux_packages
    install_source
    install_python_deps
    write_launcher
    write_desktop_entry
    log "Installed to $INSTALL_DIR"
    if [ "$RUN_AFTER_INSTALL" = "1" ]; then
        "$INSTALL_DIR/twitchfreedom.sh" >/dev/null 2>&1 &
        log "Started Twitch Freedom"
    fi
}

main "$@"
