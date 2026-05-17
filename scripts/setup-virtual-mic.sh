#!/usr/bin/env bash
# Load (or unload) a PulseAudio / PipeWire pipe-source named 'ccpipe_mic'
# that reads from /tmp/ccpipe_mic.pipe. Claude Code's /voice sees this as
# a microphone; ccpipe's backend writes 16 kHz mono Int16 PCM into the pipe.
#
# Usage:
#   setup-virtual-mic.sh [up|down]
#
# - up   : (default) load the module, replacing any existing instance
# - down : unload the module if present and remove the FIFO
#
# Idempotent: running 'up' twice has the same effect as once.

set -euo pipefail

PIPE=/tmp/ccpipe_mic.pipe
SOURCE_NAME=ccpipe_mic

unload_module() {
    local existing
    existing=$(pactl list short modules 2>/dev/null \
        | awk -v name="source_name=$SOURCE_NAME" '$2 == "module-pipe-source" && $0 ~ name {print $1}' \
        | head -n1)
    if [[ -n "$existing" ]]; then
        pactl unload-module "$existing"
    fi
}

action=${1:-up}

case "$action" in
    up)
        unload_module

        if [[ -d "$PIPE" ]]; then
            echo "ERROR: $PIPE is a directory (likely a leftover Docker bind-mount)." >&2
            echo "Remove it manually: sudo rmdir '$PIPE'" >&2
            exit 1
        fi
        rm -f "$PIPE"

        pactl load-module module-pipe-source \
            source_name="$SOURCE_NAME" \
            file="$PIPE" \
            format=s16le rate=16000 channels=1 \
            source_properties=device.description=ccpipe_browser_mic \
            >/dev/null

        echo "Virtual mic '$SOURCE_NAME' loaded; pipe at $PIPE"
        ;;

    down)
        unload_module
        rm -f "$PIPE"
        echo "Virtual mic '$SOURCE_NAME' unloaded"
        ;;

    *)
        echo "Usage: $(basename "$0") {up|down}" >&2
        exit 2
        ;;
esac
