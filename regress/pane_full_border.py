#!/usr/bin/env python3
"""Check actual client output, not just the unmodified application grid."""
import os
import fcntl
import json
import struct
import termios
import re
import shlex
import subprocess
import time
import tempfile
import unicodedata

BINARY = os.path.abspath(os.environ.get("TEST_TMUX", "../tmux"))
ENV = dict(os.environ, TERM="xterm-256color", LC_ALL="C.UTF-8")
ENV.pop("TMUX", None)
EDGE = (0, 255, 135)


class Server:
    def __init__(self, suffix):
        self.command = [BINARY, "-L", "edge-%d-%s" % (os.getpid(), suffix),
                        "-f", "/dev/null"]

    def run(self, *args):
        return subprocess.check_output(self.command + list(args), env=ENV).decode("utf-8")

    def stop(self):
        subprocess.run(self.command + ["kill-server"], env=ENV,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cells(text, width, height):
    """Decode captures, including implicit default cells beyond cellsize."""
    fg = bg = None
    reverse = False
    rows = []
    for raw in text.splitlines():
        row = []
        i = 0
        while i < len(raw):
            if raw[i] == "\033":
                if raw.startswith("\033]8;", i):
                    # Links are checked separately with capture-pane -H.
                    end = raw.find("\033\\", i)
                    assert end != -1, repr(raw[i:])
                    i = end + 2
                    continue
                match = re.match(r"\033\[([0-9;]*)m", raw[i:])
                assert match, repr(raw[i:])
                values = [int(v or 0) for v in match[1].split(";")]
                j = 0
                while j < len(values):
                    v = values[j]
                    if v == 0:
                        fg = bg = None
                        reverse = False
                    elif v == 7:
                        reverse = True
                    elif v == 27:
                        reverse = False
                    elif v == 39:
                        fg = None
                    elif v == 49:
                        bg = None
                    elif 30 <= v <= 37:
                        fg = v - 30
                    elif 40 <= v <= 47:
                        bg = v - 40
                    elif 90 <= v <= 97:
                        fg = v - 90 + 8
                    elif 100 <= v <= 107:
                        bg = v - 100 + 8
                    elif v in (38, 48):
                        if values[j + 1] == 2:
                            colour = tuple(values[j + 2:j + 5])
                            j += 4
                        else:
                            assert values[j + 1] == 5
                            colour = values[j + 2]
                            j += 2
                        if v == 38:
                            fg = colour
                        else:
                            bg = colour
                    j += 1
                i += len(match[0])
                continue
            ch = raw[i]
            i += 1
            if unicodedata.combining(ch):
                index = len(row) - 1
                while index >= 0 and row[index][0] == "":
                    index -= 1
                assert index >= 0
                old = row[index]
                row[index] = (old[0] + ch, old[1], old[2])
                continue
            f, b = (bg, fg) if reverse else (fg, bg)
            if ch == "\t":
                stop = min(width, (len(row) // 8 + 1) * 8)
                row.extend([(" ", f, b)] * (stop - len(row)))
            else:
                row.append((ch, f, b))
                if unicodedata.east_asian_width(ch) in ("W", "F"):
                    row.append(("", f, b))
        assert len(row) <= width, ("capture row too wide", len(row), width, raw)
        # -N suppresses trimming, but capture-pane still stops at cellsize.
        # Unallocated cells are grid defaults, not the last emitted SGR style.
        row.extend([(" ", None, None)] * (width - len(row)))
        rows.append(row)
    assert len(rows) <= height, ("capture too tall", len(rows), height)
    rows.extend([[(" ", None, None) for _ in range(width)]
                 for _ in range(height - len(rows))])
    return rows


inner = Server("inner")
outer = Server("outer")
streams = {}
temporary = tempfile.TemporaryDirectory(prefix="tmux-edge-")


def v1_layout(saved):
    def cell(c):
        text = "%dx%d,%d,%d" % (c["w"], c["h"], c["x"], c["y"])
        if c["t"] == "p":
            return text + "," + c["I"].lstrip("%")
        left, right = ("{", "}") if c["t"] == "h" else ("[", "]")
        return text + left + ",".join(cell(ch) for ch in c["c"]) + right
    text = cell(json.loads(saved)["L"])
    checksum = 0
    for ch in text:
        checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + ord(ch)) & 65535
    return "%04x,%s" % (checksum, text)


def panes():
    return [line.split() for line in inner.run(
        "list-panes", "-F", "#{pane_id} #{pane_left} #{pane_top} "
        "#{pane_width} #{pane_height} #{pane_active}").splitlines()]


def write(pane, data):
    if pane not in streams:
        path = os.path.join(temporary.name, pane.lstrip("%"))
        os.mkfifo(path)
        streams[pane] = os.open(path, os.O_RDWR)
        inner.run("respawn-pane", "-k", "-t", pane, "exec cat " + shlex.quote(path))
    pending = data.encode("utf-8")
    while pending:
        pending = pending[os.write(streams[pane], pending):]
    time.sleep(0.08)


def fill():
    for pane, _, _, width, height, _ in panes():
        width, height = int(width), int(height)
        data = "\033[0m\033[2J\033[?7l\033[38;2;18;52;86m\033[44m"
        for y in range(height):
            line = chr(65 + y % 26) * width
            if width >= 6:
                line = "界e\u0301" + "x" * (width - 5) + "界"
            data += "\033[%d;1H%s" % (y + 1, line)
        write(pane, data + "\033[?7h")


def check(enabled=True, status=False, scrollbar=None):
    deadline = time.monotonic() + 5
    while True:
        try:
            osx, osy = map(int, outer.run("display-message", "-p",
                                         "#{pane_width} #{pane_height}").split())
            physical = cells(outer.run("capture-pane", "-peN", "-S", "0"), osx, osy)
            ps = panes()
            wsx, wsy = map(int, inner.run("display-message", "-p",
                                         "#{window_width} #{window_height}").split())
            for pane, x, y, width, height, active in ps:
                x, y, width, height = map(int, (x, y, width, height))
                stored = cells(inner.run("capture-pane", "-peN", "-t", pane, "-S", "0"),
                               width, height)
                on = enabled and active == "1" and len(ps) > 1
                for py in range(height):
                    for px in range(width):
                        ch, fg, bg = stored[py][px]
                        expected = (ch, fg, bg)
                        got = physical[y + py][x + px]
                        assert got == expected, (pane, px, py, got, expected)
            if enabled and len(ps) > 1:
                active = next(p for p in ps if p[-1] == "1")
                ax, ay, aw, ah = map(int, active[1:5])
                if scrollbar == "left":
                    ax -= 1
                    aw += 1
                elif scrollbar == "right":
                    aw += 1
                assert ax >= 1 and ay >= 1
                assert ax + aw < wsx and ay + ah < wsy
                for by in range(wsy):
                    for bx in range(wsx):
                        if any(int(p[1]) <= bx < int(p[1]) + int(p[3]) and
                               int(p[2]) <= by < int(p[2]) + int(p[4]) for p in ps):
                            continue
                        adjacent = ((bx in (ax - 1, ax + aw) and ay - 1 <= by <= ay + ah) or
                                    (by in (ay - 1, ay + ah) and ax - 1 <= bx <= ax + aw))
                        if adjacent:
                            assert physical[by][bx][2] == EDGE, ("border", bx, by, physical[by][bx])
            return
        except (AssertionError, IndexError) as error:
            if time.monotonic() >= deadline:
                raise AssertionError("client output mismatch: %s" % (error,)) from error
            time.sleep(0.05)


try:
    inner.run("new-session", "-d", "-x", "40", "-y", "16", "exec sleep 300")
    inner.run("set-option", "-g", "status", "off")
    inner.run("set-option", "-s", "terminal-features", "*:RGB")
    outer.run("new-session", "-d", "-x", "40", "-y", "16",
              "TERM=xterm-256color " + shlex.join(inner.command + ["attach-session"]))
    outer.run("set-option", "-g", "status", "off")
    time.sleep(0.3)

    # Single panes are unchanged. Runtime toggles resize real content grids.
    inner.run("set-option", "-w", "pane-active-border-style", "bg=#00ff87")
    single = panes()
    inner.run("set-option", "-w", "pane-full-border", "on")
    assert panes() == single
    fill()
    check()
    inner.run("set-option", "-w", "pane-full-border", "off")
    inner.run("split-window", "-h", "exec sleep 300")
    baseline_geometry = panes()
    fill()
    check(False)
    inner.run("set-option", "-w", "pane-full-border", "on")
    assert panes() != baseline_geometry
    fill()
    check()
    framed = panes()
    inner.run("set-option", "-w", "pane-full-border", "off")
    fill()
    check(False)
    inner.run("set-option", "-w", "pane-full-border", "on")
    fill()
    check()
    assert panes() == framed
    geometry = [p[:5] for p in panes()]
    inner.run("select-pane", "-t", panes()[0][0])
    assert [p[:5] for p in panes()] == geometry
    check()

    # Nested layouts and all focus positions; shared separator cells are
    # checked below as well as application cells.
    inner.run("split-window", "-v", "exec sleep 300")
    fill()
    for p in panes():
        inner.run("select-pane", "-t", p[0])
        check()
    inner.run("resize-pane", "-L", "2")
    fill()
    check()
    outer.run("resize-window", "-x", "44", "-y", "18")
    time.sleep(0.2)
    fill()
    check()

    # Direct writes, clears, insertion/deletion, both directions of scrolling,
    # partial scroll regions, explicit backgrounds and alternate screens.
    active = next(p for p in panes() if p[-1] == "1")
    pane, _, _, width, height, _ = active
    width, height = int(width), int(height)
    updates = [
        "\033[2J\033[H\033[44mhello",
        "\033[?1049h\033[44m\033[2J\033[H界e\u0301",
        "\033[2;1H\033[K\tZ",
        "\033[2;1H\033[2@AB\033[P",
        "\033[1;1H\033[2L\033[M",
        "\033[1;1H\033[2T\033[2S",
        "\033[2;%dr\033[2S\033[T\033[r" % max(3, height - 1),
        "\033[H" + ("\033[44mrapid scrolling output\r\n" * 300),
        "\033[?1049l",
        "\033[44m\033[2J\033[H\033]8;;https://example.com/edge\033\\link\033]8;;\033\\",
    ]
    for update in updates:
        write(pane, update)
        check()
    assert "https://example.com/edge" in outer.run("capture-pane", "-pH")

    # Returning from a wide edge glyph to a narrow one must remove its extra
    # highlighted cell, not leave a two-cell strip inside the pane.
    write(pane, "\033[2;1H界\033[2;1HX")
    check()
    for border in ("single", "double", "heavy", "rounded"):
        inner.run("set-option", "-w", "pane-border-lines", border)
        check()
    # All preset constructors and both serialization formats reserve the frame.
    for layout in ("even-horizontal", "even-vertical", "main-horizontal",
                   "main-horizontal-mirrored", "main-vertical",
                   "main-vertical-mirrored", "tiled"):
        inner.run("select-layout", layout)
        fill()
        check()
        saved = inner.run("display-message", "-p", "#{window_layout}").strip()
        for restored in (saved, v1_layout(saved)):
            inner.run("select-layout", restored)
            fill()
            check()
    for p in panes():
        tty = inner.run("display-message", "-p", "-t", p[0], "#{pane_tty}").strip()
        fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY)
        try:
            rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(
                fd, termios.TIOCGWINSZ, bytes(8)))
            assert (cols, rows) == tuple(map(int, p[3:5]))
        finally:
            os.close(fd)
    inner.run("set-option", "-w", "pane-border-format", "界e\u0301 #{pane_index}")
    for status in ("top", "bottom", "off"):
        inner.run("set-option", "-w", "pane-border-status", status)
        fill()
        for p in panes():
            inner.run("select-pane", "-t", p[0])
            check(status=status != "off")

    for side in ("left", "right"):
        inner.run("set-option", "-w", "pane-scrollbars", "on")
        inner.run("set-option", "-w", "pane-scrollbars-style", "width=1,pad=0")
        inner.run("set-option", "-w", "pane-scrollbars-position", side)
        fill()
        check(scrollbar=side)
    inner.run("set-option", "-w", "pane-scrollbars", "off")
    fill()

    # Scrolling every pane exercises both directions at outer and shared
    # edges, including the two nested panes whose heights differ.
    for p in panes():
        inner.run("select-pane", "-t", p[0])
        write(p[0], "\033[44m\033[H" + "line\r\n" * 100)
        check()
        write(p[0], "\033[H\033[2T")
        check()
        write(p[0], "\033[2;1H\033[0m\033[7mreverse\033[0m")
        check()
    inner.run("set-option", "-w", "pane-full-border", "off")
    fill()
    check(False)
    inner.run("set-option", "-w", "pane-full-border", "on")
    for p in panes()[:-1]:
        inner.run("kill-pane", "-t", p[0])
    check()
    fill()
    assert panes()[0][1:5] == ["0", "0", "44", "18"]
    inner.run("split-window", "-v", "exec sleep 300")
    fill()
    check()
    # Zoom uses a single full-sized content pane; unzoom restores the frame.
    inner.run("resize-pane", "-Z")
    assert inner.run("display-message", "-p", "#{pane_left} #{pane_top} #{pane_width} #{pane_height}").strip() == "0 0 44 18"
    inner.run("resize-pane", "-Z")
    fill()
    check()

    small = Server("minimum")
    try:
        small.run("new-session", "-d", "-x", "5", "-y", "3", "exec sleep 300")
        small.run("set-option", "-g", "status", "off")
        small.run("set-option", "-w", "pane-full-border", "on")
        small.run("split-window", "-h", "exec sleep 300")
        assert small.run("list-panes", "-F", "#{pane_width} #{pane_height}").splitlines() == ["1 1", "1 1"]
        result = subprocess.run(small.command + ["split-window", "-h", "exec sleep 300"],
                                env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert result.returncode != 0
        small.run("resize-pane", "-L", "100")
        assert small.run("list-panes", "-F", "#{pane_width} #{pane_height}").splitlines() == ["1 1", "1 1"]
        small.run("kill-pane")
        assert small.run("list-panes", "-F", "#{pane_left} #{pane_top} #{pane_width} #{pane_height}").strip() == "0 0 5 3"
    finally:
        small.stop()
    print("pane-full-border: PASS")
finally:
    outer.stop()
    inner.stop()
    for descriptor in streams.values():
        os.close(descriptor)
    temporary.cleanup()
