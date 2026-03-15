#!/usr/bin/env python3
"""
RTPI Departure Board — lightweight curses display for Raspberry Pi / TTY.
Fetches real-time departure data from stops.lt API and renders a departure
board on the terminal, designed for a Waveshare 3.2" display (~40x15 chars).
"""

import configparser
import contextlib
import curses
import datetime
import locale
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class VehicleType(StrEnum):
    TROL = "trol"
    BUS = "bus"
    EXPRESS = "expressbus"


class Departure(NamedTuple):
    type: VehicleType
    route: str
    direction: str
    dep_secs: int
    vehicle_id: str
    destination: str


@dataclass
class AppState:
    departures: list = field(default_factory=list)
    stop_id: str = ""
    stop_name: str | None = None
    last_updated: datetime.datetime | None = None
    error_msg: str | None = None
    refreshing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)


# ---------------------------------------------------------------------------
# Curses color pair IDs
# ---------------------------------------------------------------------------


class ColorPair(IntEnum):
    # fmt: off
    TROL = 1        # red    — trolleybus  rgb(220, 49, 49)
    BUS = 2         # blue   — bus         rgb(0, 115, 172)
    EXPRESS = 3     # green  — express bus rgb(0, 128, 0)
    HEADER = 4      # white bold — header / column titles
    SEP = 5         # dim white — separator lines
    DUE = 6         # red bold  — imminent departure ("Due")
    ERROR = 7       # red       — error indicator
    STATUS = 8      # dim       — status bar text
    TROL_INV = 11   # white on red   — route badge
    BUS_INV = 12    # white on blue  — route badge
    EXPRESS_INV = 13  # white on green — route badge
    # fmt: on


# Custom color slot numbers (used when terminal supports init_color)
class ColorSlot(IntEnum):
    TROL = 8
    BUS = 9
    EXPRESS = 10


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "display": {
        "stop_id": "0410",
        "refresh_interval": "30",
        "max_departures": "20",
        "timezone": "Europe/Vilnius",
    },
    "api": {
        "base_url": "https://www.stops.lt/vilnius/departures2.php",
        "stops_url": "https://www.stops.lt/vilnius/vilnius/stops.txt",
        "timeout": "10",
    },
}


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(_DEFAULTS)
    cfg.read(path)
    return cfg


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def build_url(cfg: configparser.ConfigParser) -> str:
    base = cfg.get("api", "base_url")
    stop_id = cfg.get("display", "stop_id")
    ms = int(time.time() * 1000)
    return f"{base}?stopid={stop_id}&time={ms}"


def fetch_raw(url: str, timeout: int) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def parse_response(raw: bytes) -> tuple:
    """Return (stop_id, list[Departure])."""
    text = raw.decode("utf-8", errors="replace")
    departures = []
    stop_id = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(",")
        if parts[0] == "stop":
            if len(parts) >= 2:
                stop_id = parts[1].strip()
            continue
        if len(parts) < 6:
            continue
        try:
            dep = Departure(
                type=VehicleType(parts[0].strip()),
                route=parts[1].strip(),
                direction=parts[2].strip(),
                dep_secs=int(parts[3].strip()),
                vehicle_id=parts[4].strip(),
                destination=parts[5].strip(),
            )
            departures.append(dep)
        except (ValueError, IndexError):
            continue
    return stop_id, departures


def build_stops_url(cfg: configparser.ConfigParser, tz: datetime.tzinfo) -> str:
    base = cfg.get("api", "stops_url")
    now = datetime.datetime.now(tz)
    t = now.replace(minute=0, second=0, microsecond=0)
    ms = int(t.timestamp() * 1000)
    return f"{base}?{ms}"


def parse_stop_name(raw: bytes, stop_id: str) -> str | None:
    """Return the name field for stop_id from stops.txt, or None if not found."""
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) >= 6 and parts[0] == stop_id:
            return parts[5].strip() or None
    return None


def fetch_stop_name(
    cfg: configparser.ConfigParser, stop_id: str, tz: datetime.tzinfo
) -> str | None:
    url = build_stops_url(cfg, tz)
    timeout = cfg.getint("api", "timeout")
    raw = fetch_raw(url, timeout)
    return parse_stop_name(raw, stop_id)


# ---------------------------------------------------------------------------
# Time / display helpers
# ---------------------------------------------------------------------------


def seconds_since_midnight(tz: datetime.tzinfo) -> int:
    now = datetime.datetime.now(tz)
    return now.hour * 3600 + now.minute * 60 + now.second


def format_due(dep_secs: int, tz: datetime.tzinfo) -> str:
    diff = dep_secs - seconds_since_midnight(tz)
    # Midnight rollover: huge negative means next-day departure
    if diff < -300:
        diff += 86400
    minutes = diff // 60
    if minutes < 1:
        return "Due"
    if minutes < 60:
        return f"{minutes}m"
    # Show actual time for distant departures
    now = datetime.datetime.now(tz)
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    dep_time = base + datetime.timedelta(seconds=dep_secs)
    return dep_time.strftime("%H:%M")


def type_char(t: VehicleType) -> str:
    return {
        VehicleType.TROL: "T",
        VehicleType.BUS: "B",
        VehicleType.EXPRESS: "E",
    }.get(t, "?")


def color_pair_for_type(t: VehicleType) -> ColorPair:
    return {
        VehicleType.TROL: ColorPair.TROL,
        VehicleType.BUS: ColorPair.BUS,
        VehicleType.EXPRESS: ColorPair.EXPRESS,
    }.get(t, ColorPair.BUS)


def color_pair_inv_for_type(t: VehicleType) -> ColorPair:
    return {
        VehicleType.TROL: ColorPair.TROL_INV,
        VehicleType.BUS: ColorPair.BUS_INV,
        VehicleType.EXPRESS: ColorPair.EXPRESS_INV,
    }.get(t, ColorPair.BUS_INV)


# ---------------------------------------------------------------------------
# Background fetch thread
# ---------------------------------------------------------------------------


def fetch_and_update(cfg: configparser.ConfigParser, state: AppState) -> None:
    timeout = cfg.getint("api", "timeout")
    url = build_url(cfg)
    try:
        raw = fetch_raw(url, timeout)
        stop_id, deps = parse_response(raw)
        with state.lock:
            state.departures = deps
            if stop_id:
                state.stop_id = stop_id
            state.last_updated = datetime.datetime.now(datetime.UTC)
            state.error_msg = None
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        with state.lock:
            state.error_msg = reason[:24]
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error_msg = type(exc).__name__[:24]


def background_worker(
    cfg: configparser.ConfigParser, state: AppState, tz: datetime.tzinfo
) -> None:
    interval = cfg.getint("display", "refresh_interval")
    # Fetch stop name once at startup
    with contextlib.suppress(Exception):
        name = fetch_stop_name(cfg, state.stop_id, tz)
        with state.lock:
            state.stop_name = name
    # First departure fetch immediately
    state.refreshing = True
    fetch_and_update(cfg, state)
    state.refreshing = False
    # Then loop
    while not state.stop_event.wait(interval):
        state.refreshing = True
        fetch_and_update(cfg, state)
        state.refreshing = False


# ---------------------------------------------------------------------------
# Curses drawing
# ---------------------------------------------------------------------------


def _rgb(r: int, g: int, b: int) -> tuple:
    """Convert 0-255 RGB to curses 0-1000 scale."""
    return int(r / 255 * 1000), int(g / 255 * 1000), int(b / 255 * 1000)


def init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()

    # Use exact brand RGB values when the terminal supports custom colors
    # (e.g. xterm-256color over SSH). Falls back to nearest standard color
    # on TERM=linux framebuffer console (Pi 1 with Waveshare display).
    if curses.can_change_color() and curses.COLORS >= 16:
        curses.init_color(ColorSlot.TROL, *_rgb(220, 49, 49))  # red
        curses.init_color(ColorSlot.BUS, *_rgb(0, 115, 172))  # blue
        curses.init_color(ColorSlot.EXPRESS, *_rgb(0, 128, 0))  # green
        trol_color = ColorSlot.TROL
        bus_color = ColorSlot.BUS
        express_color = ColorSlot.EXPRESS
    else:
        trol_color = curses.COLOR_RED
        bus_color = curses.COLOR_BLUE
        express_color = curses.COLOR_GREEN

    curses.init_pair(ColorPair.TROL, trol_color, -1)
    curses.init_pair(ColorPair.BUS, bus_color, -1)
    curses.init_pair(ColorPair.EXPRESS, express_color, -1)
    curses.init_pair(ColorPair.TROL_INV, curses.COLOR_WHITE, trol_color)
    curses.init_pair(ColorPair.BUS_INV, curses.COLOR_WHITE, bus_color)
    curses.init_pair(ColorPair.EXPRESS_INV, curses.COLOR_WHITE, express_color)
    curses.init_pair(ColorPair.HEADER, curses.COLOR_WHITE, -1)
    curses.init_pair(ColorPair.SEP, curses.COLOR_WHITE, -1)
    curses.init_pair(ColorPair.DUE, curses.COLOR_RED, -1)
    curses.init_pair(ColorPair.ERROR, curses.COLOR_RED, -1)
    curses.init_pair(ColorPair.STATUS, curses.COLOR_WHITE, -1)


def safe_addstr(
    win: curses.window,
    row: int,
    col: int,
    text: str,
    attr: int = 0,
) -> None:
    """addstr wrapper that silently ignores out-of-bounds writes."""
    try:
        max_rows, max_cols = win.getmaxyx()
        if row < 0 or row >= max_rows or col < 0 or col >= max_cols:
            return
        available = max_cols - col - 1  # leave last cell of last row untouched
        if row < max_rows - 1:
            available = max_cols - col
        if available <= 0:
            return
        win.addstr(row, col, text[:available], attr)
    except curses.error:
        pass


def draw_separator(win: curses.window, row: int, cols: int) -> None:
    safe_addstr(win, row, 0, "\u2500" * (cols - 1), curses.color_pair(ColorPair.SEP))


def draw_screen(
    stdscr: curses.window,
    *,
    stop_id: str,
    stop_name: str | None,
    departures: list[Departure],
    last_updated: datetime.datetime | None,
    error_msg: str | None,
    refreshing: bool,
    max_departures: int,
    tz: datetime.tzinfo,
) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()

    if rows < 5 or cols < 20:
        safe_addstr(stdscr, 0, 0, "Terminal too small", curses.A_BOLD)
        return

    bold = curses.color_pair(ColorPair.HEADER) | curses.A_BOLD
    dim = curses.color_pair(ColorPair.SEP)

    # --- Row 0: title + updated time ---
    title = f" {stop_name}" if stop_name else f" Stop {stop_id or '?'}"
    if error_msg:
        status_right = f"ERR:{error_msg}"
        status_attr = curses.color_pair(ColorPair.ERROR) | curses.A_BOLD
    elif last_updated:
        status_right = f"Updated {last_updated.strftime('%H:%M:%S')}"
        status_attr = dim
    else:
        status_right = "Connecting..."
        status_attr = dim

    safe_addstr(stdscr, 0, 0, title, bold)
    right_col = max(len(title) + 1, cols - len(status_right) - 1)
    safe_addstr(stdscr, 0, right_col, status_right, status_attr)

    # --- Row 1: separator ---
    draw_separator(stdscr, 1, cols)

    # --- Row 2: column headers ---
    # Layout: " T  Rte  Destination...  Due"  # noqa: ERA001
    #          0 1  3    7              cols-5
    due_width = 5  # " Due " or "  14m" or "15:30"
    route_width = 4  # up to 4 chars (e.g. "1g", "88", "10")
    # dest starts at col 8, ends at cols - due_width - 1
    dest_col = 8
    dest_width = max(1, cols - dest_col - due_width - 1)
    due_col = cols - due_width - 1

    hdr_attr = bold
    safe_addstr(stdscr, 2, 1, "T", hdr_attr)
    safe_addstr(stdscr, 2, 3, "Rte", hdr_attr)
    safe_addstr(stdscr, 2, dest_col, "Destination", hdr_attr)
    due_label = "Due"
    safe_addstr(stdscr, 2, due_col + due_width - len(due_label), due_label, hdr_attr)

    # --- Row 3: separator ---
    draw_separator(stdscr, 3, cols)

    # --- Rows 4..rows-2: departure rows ---
    data_rows = rows - 5  # rows 4 to rows-2 inclusive
    visible = departures[: min(max_departures, data_rows)]

    if not visible and last_updated is not None:
        safe_addstr(stdscr, 4, 2, "No departures", dim)
    elif not visible:
        safe_addstr(stdscr, 4, 2, "Fetching...", dim)

    for i, dep in enumerate(visible):
        row = 4 + i
        if row >= rows - 1:
            break

        due_str = format_due(dep.dep_secs, tz)
        t_char = type_char(dep.type)
        type_attr = curses.color_pair(color_pair_for_type(dep.type))
        plain = curses.color_pair(ColorPair.HEADER)
        due_attr = (
            curses.color_pair(ColorPair.DUE) | curses.A_BOLD
            if due_str == "Due"
            else plain
        )

        # Type char — colored + bold
        safe_addstr(stdscr, row, 1, t_char, type_attr | curses.A_BOLD)

        # Route — white text on solid transport-color block
        route_padded = dep.route[:route_width].rjust(route_width)
        inv_attr = curses.color_pair(color_pair_inv_for_type(dep.type)) | curses.A_BOLD
        safe_addstr(stdscr, row, 3, route_padded, inv_attr)

        # Destination — plain
        dest = dep.destination[:dest_width]
        safe_addstr(stdscr, row, dest_col, dest, plain)

        # Due — plain (red bold if imminent)
        safe_addstr(stdscr, row, due_col, due_str.rjust(due_width), due_attr)

    # --- Row rows-2: separator ---
    draw_separator(stdscr, rows - 2, cols)

    # --- Row rows-1: status bar ---
    if refreshing:
        left_status = " Refreshing..."
    else:
        left_status = ""
    right_status = "[q] quit "
    safe_addstr(stdscr, rows - 1, 0, left_status, dim)
    safe_addstr(stdscr, rows - 1, cols - len(right_status) - 1, right_status, dim)


# ---------------------------------------------------------------------------
# Main curses entry point
# ---------------------------------------------------------------------------


def main(stdscr: curses.window, cfg: configparser.ConfigParser) -> None:
    for locale_ in ("", "C.UTF-8", "C"):
        try:
            locale.setlocale(locale.LC_ALL, locale_)
            break
        except locale.Error:
            continue

    curses.curs_set(0)
    stdscr.nodelay(True)  # noqa: FBT003
    stdscr.timeout(500)
    init_colors()

    stop_id = cfg.get("display", "stop_id")
    max_dep = cfg.getint("display", "max_departures")
    try:
        tz = ZoneInfo(cfg.get("display", "timezone"))
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")

    state = AppState(stop_id=stop_id)

    # Handle SIGTERM (systemd stop) gracefully
    def _sigterm(_signum: int, _frame: object) -> None:
        state.stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # Start background fetch thread
    worker = threading.Thread(
        target=background_worker, args=(cfg, state, tz), daemon=True
    )
    worker.start()

    # Draw loop
    while not state.stop_event.is_set():
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):  # q, Q, ESC
            state.stop_event.set()
            break

        # Snapshot state under lock
        with state.lock:
            departures = list(state.departures)
            sid = state.stop_id
            sname = state.stop_name
            updated = state.last_updated
            error = state.error_msg
        refreshing = state.refreshing

        draw_screen(
            stdscr,
            stop_id=sid,
            stop_name=sname,
            departures=departures,
            last_updated=updated,
            error_msg=error,
            refreshing=refreshing,
            max_departures=max_dep,
            tz=tz,
        )
        stdscr.refresh()

    # Clean shutdown
    state.stop_event.set()
    worker.join(timeout=2)
    curses.endwin()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    config_path = Path(__file__).resolve().parent / "config.ini"
    cfg = load_config(config_path)
    with contextlib.suppress(KeyboardInterrupt):
        curses.wrapper(main, cfg)


if __name__ == "__main__":
    run()
