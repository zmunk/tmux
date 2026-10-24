#!/bin/sh
# Check popup content and PTY sizes through a real attached nested client.
command -v python3 >/dev/null 2>&1 || exit 101
exec python3 ./popup-geometry.py
