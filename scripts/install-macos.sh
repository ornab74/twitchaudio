#!/usr/bin/env bash
set -euo pipefail

APP_NAME="TwitchFreedom"
REPO_OWNER="${TWITCHFREEDOM_REPO_OWNER:-ornab74}"
REPO_NAME="${TWITCHFREEDOM_REPO_NAME:-twitchfreedom}"
REPO_REF="${TWITCHFREEDOM_REPO_REF:-main}"
INSTALL_DIR="${TWITCHFREEDOM_INSTALL_DIR:-$HOME/Applications/TwitchFreedom}"
BIN_DIR="${TWITCHFREEDOM_BIN_DIR:-$HOME/bin}"
CREATE_APP="${TWITCHFREEDOM_CREATE_APP:-1}"
RUN_AFTER_INSTALL="${TWITCHFREEDOM_RUN_AFTER_INSTALL:-1}"
SOURCE_DIR="${TWITCHFREEDOM_SOURCE_DIR:-}"

log() {
    printf '[%s] %s\n' "$APP_NAME" "$*"
}

die() {
    printf '[%s] ERROR: %s\n' "$APP_NAME" "$*" >&2
    exit 1
}

install_macos_packages() {
    if ! command -v brew >/dev/null 2>&1; then
        if ! command -v python3 >/dev/null 2>&1 || ! command -v ffplay >/dev/null 2>&1; then
            die "Homebrew is not installed and Python 3 or FFmpeg is missing. Install Homebrew from https://brew.sh, then rerun this script."
        fi
        return
    fi

    command -v python3 >/dev/null 2>&1 || brew install python
    command -v ffplay >/dev/null 2>&1 || brew install ffmpeg
    command -v curl >/dev/null 2>&1 || brew install curl

    if ! python3 -c "import tkinter" >/dev/null 2>&1; then
        log "Python Tk support is missing; trying Homebrew python-tk packages"
        brew install python-tk >/dev/null 2>&1 || brew install python-tk@3.12 >/dev/null 2>&1 || true
    fi
}

download_file() {
    local url="$1"
    local output="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" -o "$output"
    else
        die "curl is required to download Twitch Freedom."
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

write_command_launcher() {
    local runner="$INSTALL_DIR/TwitchFreedom.command"
    mkdir -p "$BIN_DIR"
    cat > "$runner" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/main.py" "\$@"
EOF
    chmod +x "$runner"
    ln -sf "$runner" "$BIN_DIR/twitchfreedom"
    log "Command launcher: $runner"
    log "Shell launcher: $BIN_DIR/twitchfreedom"
}

write_app_bundle() {
    [ "$CREATE_APP" = "1" ] || return

    local app_bundle macos_dir resources_dir executable plist
    app_bundle="$HOME/Applications/TwitchFreedom.app"
    macos_dir="$app_bundle/Contents/MacOS"
    resources_dir="$app_bundle/Contents/Resources"
    executable="$macos_dir/TwitchFreedom"
    plist="$app_bundle/Contents/Info.plist"

    mkdir -p "$macos_dir" "$resources_dir"
    if [ -f "$INSTALL_DIR/logo.png" ]; then
        cp "$INSTALL_DIR/logo.png" "$resources_dir/twitchfreedom.png"
    fi

    cat > "$executable" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
exec "$INSTALL_DIR/.venv/bin/python" "$INSTALL_DIR/main.py"
EOF
    chmod +x "$executable"

    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>TwitchFreedom</string>
    <key>CFBundleIdentifier</key>
    <string>com.ornab74.twitchfreedom</string>
    <key>CFBundleName</key>
    <string>TwitchFreedom</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
</dict>
</plist>
EOF
    log "macOS app launcher: $app_bundle"
}

main() {
    install_macos_packages
    install_source
    install_python_deps
    write_command_launcher
    write_app_bundle
    log "Installed to $INSTALL_DIR"
    if [ "$RUN_AFTER_INSTALL" = "1" ]; then
        if [ "$CREATE_APP" = "1" ]; then
            open "$HOME/Applications/TwitchFreedom.app"
        else
            "$INSTALL_DIR/TwitchFreedom.command" >/dev/null 2>&1 &
        fi
        log "Started Twitch Freedom"
    fi
}

main "$@"
