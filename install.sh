#!/usr/bin/env bash
# Build the EOL Tester into a desktop program and install it on this PC.
#
# Run from Git Bash on the tester PC:
#
#     ./install.sh                       # build, install, add shortcuts
#     ./install.sh --dir "D:/EOL Tester" # install somewhere else
#     ./install.sh --build-only          # build into build/dist only
#
# The program reads its settings from the files beside it: .config,
# camera_cfg.ini, vision_config.json, the PLC address and employee code
# .txt files, the label .prn files and vision_models/. They are copied in
# from this folder on the first install only; a reinstall keeps the ones
# the machine already has, so its COM ports, cameras and models survive an
# update. Pass --reset-config to copy them in again.
#
# The database settings are in db.py and are built into the program.

set -euo pipefail

APP_NAME="EOL Tester"
EXE_NAME="EOL_Tester"
ENTRY="mainconsole.py"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$PROJECT_DIR/build"
VENV_DIR="$BUILD_DIR/venv"
DIST_DIR="$BUILD_DIR/dist/$EXE_NAME"

INSTALL_DIR="${LOCALAPPDATA:-$HOME/AppData/Local}/Programs/$APP_NAME"
BUILD_ONLY=false
RESET_CONFIG=false

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) INSTALL_DIR="$2"; shift 2 ;;
        --build-only) BUILD_ONLY=true; shift ;;
        --reset-config) RESET_CONFIG=true; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
    esac
done

step() { echo; echo "==> $*"; }

# Machine settings and data the program reads from beside itself
CONFIG_FILES=(.config camera_cfg.ini vision_config.json plc_config.json
              process_monitor_config.json)
CONFIG_DIRS=(vision_models)
# Shipped with every install, overwriting the old copy
APP_DIRS=(assets)

cd "$PROJECT_DIR"

# --- Python -------------------------------------------------------------
step "Finding Python 3"
PYTHON=""
for candidate in "py -3" python3 python; do
    if $candidate -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Python 3.10 or newer was not found. Install it from python.org" \
         "(tick 'tcl/tk' and 'Add to PATH'), then run this again." >&2
    exit 1
fi
$PYTHON --version

# --- Build environment ----------------------------------------------------
step "Preparing the build environment in build/venv"
if [ ! -d "$VENV_DIR" ]; then
    $PYTHON -m venv "$VENV_DIR"
fi
if [ -x "$VENV_DIR/Scripts/python.exe" ]; then
    VPY="$VENV_DIR/Scripts/python.exe"
else
    VPY="$VENV_DIR/bin/python"
fi
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r requirements.txt "pyinstaller>=6.0"

# --- Build --------------------------------------------------------------
# One folder, with the libraries beside the .exe (--contents-directory .):
# every module finds its data files next to itself, so the settings files
# sit next to EOL_Tester.exe where they can be edited.
step "Building $EXE_NAME.exe"
"$VPY" -m PyInstaller --noconfirm --clean --windowed --onedir \
    --name "$EXE_NAME" \
    --contents-directory . \
    --distpath build/dist --workpath build/work --specpath build \
    --collect-all customtkinter \
    --hidden-import babel.numbers \
    --hidden-import pynput.keyboard._win32 \
    --hidden-import pynput.mouse._win32 \
    "$ENTRY"

for dir in "${APP_DIRS[@]}"; do
    [ -d "$dir" ] && cp -r "$dir" "$DIST_DIR/"
done
echo "Built: $DIST_DIR"

if $BUILD_ONLY; then
    exit 0
fi

# --- Install --------------------------------------------------------------
step "Installing to $INSTALL_DIR"
if tasklist 2>/dev/null | grep -qi "^$EXE_NAME.exe"; then
    echo "$APP_NAME is running. Close it, then run this again." >&2
    exit 1
fi
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/logs"
cp -r "$DIST_DIR/." "$INSTALL_DIR/"

# Copied only when missing, so a reinstall keeps this machine's settings
copy_config() {
    local src="$1"
    local dest="$INSTALL_DIR/$(basename "$src")"
    if $RESET_CONFIG || [ ! -e "$dest" ]; then
        cp -r "$src" "$INSTALL_DIR/"
        echo "  copied   $(basename "$src")"
    else
        echo "  kept     $(basename "$src")"
    fi
}
for file in "${CONFIG_FILES[@]}"; do
    [ -f "$file" ] && copy_config "$file"
done
for dir in "${CONFIG_DIRS[@]}"; do
    [ -d "$dir" ] && copy_config "$dir"
done
# PLC address lists, employee codes, part labels and label print files
for file in *.txt *.prn; do
    [ -f "$file" ] || continue
    [ "$file" = "requirements.txt" ] && continue
    copy_config "$file"
done

# --- Shortcuts ----------------------------------------------------------
step "Adding Desktop and Start Menu shortcuts"
if command -v cygpath >/dev/null && command -v powershell.exe >/dev/null; then
    EXE_WIN="$(cygpath -w "$INSTALL_DIR/$EXE_NAME.exe")"
    DIR_WIN="$(cygpath -w "$INSTALL_DIR")"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
        \$shell = New-Object -ComObject WScript.Shell
        \$places = @([Environment]::GetFolderPath('Desktop'),
                     [Environment]::GetFolderPath('Programs'))
        foreach (\$place in \$places) {
            \$link = \$shell.CreateShortcut((Join-Path \$place '$APP_NAME.lnk'))
            \$link.TargetPath = '$EXE_WIN'
            \$link.WorkingDirectory = '$DIR_WIN'
            \$link.Description = 'End of line tester'
            \$link.Save()
        }"
    echo "  Desktop and Start Menu: $APP_NAME"
else
    echo "  Skipped: run this from Git Bash on Windows to get shortcuts."
fi

step "Done"
echo "Start $APP_NAME from its Desktop shortcut, or run:"
echo "  $INSTALL_DIR/$EXE_NAME.exe"
echo "Settings are in $INSTALL_DIR (.config, camera_cfg.ini, ...)."
