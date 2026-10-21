#!/bin/sh
# Exercise real client rendering through a nested tmux terminal.
command -v python3 >/dev/null 2>&1 || exit 101
exec python3 ./pane_full_border.py
