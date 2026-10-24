#!/bin/sh
# MediaWave1337 setup for Linux: finds Python 3 + tkinter, then opens the setup wizard.
cd "$(dirname "$0")" || exit 1

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

install_hint() {
    if command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y $1"
    elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install -y $2"
    elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S --needed $3"
    elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install $4"
    fi
}

offer_install() {
    cmd="$1"
    [ -z "$cmd" ] && return 1
    echo "It can be installed with:"
    echo "    $cmd"
    printf "Run this now? [y/N] "
    read -r answer
    case "$answer" in
        y|Y|yes|YES) sh -c "$cmd" ;;
        *) return 1 ;;
    esac
}

if [ -z "$PYTHON" ]; then
    echo "MediaWave1337 needs Python 3.9 or newer, which was not found."
    offer_install "$(install_hint python3 python3 python python3)" || exit 1
    PYTHON=python3
fi

if ! "$PYTHON" -c 'import tkinter' 2>/dev/null; then
    echo "The setup wizard needs tkinter (Python's built-in GUI toolkit), which is not installed."
    offer_install "$(install_hint python3-tk python3-tkinter tk python3-tk)" || exit 1
fi

exec "$PYTHON" setup_wizard.py "$@"
