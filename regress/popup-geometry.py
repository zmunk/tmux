#!/usr/bin/env python3
"""Popup content/PTY geometry, including full-border and zoom transitions."""
import os
import shlex
import subprocess
import tempfile
import time

BINARY = os.path.abspath(os.environ.get("TEST_TMUX", "../tmux"))
ENV = dict(os.environ, TERM="xterm-256color")
ENV.pop("TMUX", None)


def wait_for(fn):
    deadline = time.monotonic() + 5
    while True:
        result = fn()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


with tempfile.TemporaryDirectory(prefix="popup-geometry-") as tmp:
    base = [BINARY, "-L", "popup-geometry-%d" % os.getpid(), "-f", "/dev/null"]
    outer = [BINARY, "-L", "popup-geometry-outer-%d" % os.getpid(), "-f", "/dev/null"]

    def run(*args):
        return subprocess.check_output(base + list(args), env=ENV).decode().strip()

    def geometry(pane):
        return tuple(map(int, run("display-message", "-p", "-t", pane,
                                 "#{pane_left} #{pane_top} #{pane_width} #{pane_height}").split()))

    try:
        run("new-session", "-d", "-x", "80", "-y", "24", "exec sleep 300")
        run("set", "-g", "status", "off")
        subprocess.check_call(outer + ["new-session", "-d", "-x", "80", "-y", "24",
                                      shlex.join(base + ["attach-session"])], env=ENV)
        subprocess.check_call(outer + ["set", "-g", "status", "off"], env=ENV)
        wait_for(lambda: run("list-clients", "-F", "#{client_name}"))
        for enabled in ("on", "off"):
            run("set", "-w", "pane-full-border", enabled)
            for status, position in (("off", "bottom"), ("on", "top"),
                                     ("on", "bottom"), ("2", "top")):
                run("set", "-g", "status-position", position)
                run("set", "-g", "status", status)
                for layout in ("single", "horizontal", "vertical", "zoom"):
                    target = run("list-panes", "-F", "#{pane_id}").splitlines()[0]
                    if layout != "single":
                        run("split-window", "-h" if layout != "vertical" else "-v",
                            "-t", target, "exec sleep 300")
                    if layout == "zoom":
                        run("resize-pane", "-Z", "-t", target)
                    targets = [target] if layout in ("single", "zoom") else run(
                        "list-panes", "-F", "#{pane_id}").splitlines()
                    for target in targets:
                        for borderless in (True, False):
                            before = {p: geometry(p) for p in run("list-panes", "-F", "#{pane_id}").splitlines()}
                            x, y, width, height = before[target]
                            output = os.path.join(tmp, "pty")
                            if os.path.exists(output):
                                os.unlink(output)
                            command = "stty size > %s; exec sleep 300" % shlex.quote(output)
                            args = ["display-popup", "-E", "-t", target, "-w", str(width),
                                    "-h", str(height), "-x", "P", "-y", "P"]
                            if borderless:
                                args.append("-B")
                            proc = subprocess.Popen(base + args + [command], env=ENV)
                            try:
                                wait_for(lambda: os.path.exists(output) and os.path.getsize(output))
                                popup = next(p for p in run("list-panes", "-F", "#{pane_id}").splitlines() if p not in before)
                                pad = 0 if borderless else 1
                                expected = (x + pad, y + pad, width - 2 * pad, height - 2 * pad)
                                actual = geometry(popup)
                                label = (enabled, status, position, layout, borderless)
                                assert actual == expected, (label, actual, expected)
                                with open(output) as f:
                                    pty = tuple(map(int, f.read().split()))
                                assert pty == (expected[3], expected[2]), (label, pty, expected)
                                assert {p: geometry(p) for p in before} == before, (label, "underlying panes resized")
                            finally:
                                run("display-popup", "-C")
                                proc.wait(timeout=5)
                            assert {p: geometry(p) for p in before} == before
                    if layout == "zoom":
                        run("resize-pane", "-Z", "-t", target)
                    for pane in run("list-panes", "-F", "#{pane_id}").splitlines()[1:]:
                        run("kill-pane", "-t", pane)
        print("popup geometry: passed")
    finally:
        for server in (base, outer):
            subprocess.run(server + ["kill-server"], env=ENV, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
