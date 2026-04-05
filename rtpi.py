#!/usr/bin/env python3
"""
RTPI Departure Board — lightweight curses display for Raspberry Pi / TTY.
Fetches real-time departure data from stops.lt API and renders a departure
board on the terminal, designed for a Waveshare 3.2" display (~40x15 chars).
"""

import argparse
import configparser
import contextlib
import curses
import datetime
import locale
import os
import select
import signal
import subprocess  # noqa: S404
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from io import TextIOWrapper

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class VehicleType(StrEnum):
    TROL = "trol"
    BUS = "bus"
    EXPRESS = "expressbus"
    NIGHT = "nightbus"


class StopInfo(NamedTuple):
    stop_id: str
    name: str
    direction: str
    lat: str
    lng: str


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
    stop_ids: list[str] = field(default_factory=list)
    stop_index: int = 0
    stop_name: str | None = None
    stop_direction: str | None = None
    last_updated: datetime.datetime | None = None
    error_msg: str | None = None
    refreshing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    page_offset: int = 0
    stop_event: threading.Event = field(default_factory=threading.Event)
    refresh_now: threading.Event = field(default_factory=threading.Event)

    def current_stop_id(self) -> str:
        if self.stop_ids:
            return self.stop_ids[self.stop_index % len(self.stop_ids)]
        return ""


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
    NIGHT = 14      # dark gray — night bus rgb(48, 48, 48)
    NIGHT_INV = 15  # white on dark gray — route badge
    # fmt: on


# Custom color slot numbers (used when terminal supports init_color)
class ColorSlot(IntEnum):
    TROL = 8
    BUS = 9
    EXPRESS = 10
    NIGHT = 11


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "display": {
        "city": "vilnius",
        "stop_ids": "0103, 0104",  # Europos aikštė (eastbound, westbound)
        "refresh_interval": "30",
        "timezone": "Europe/Vilnius",
    },
    "api": {
        "base_url": "",
        "stops_url": "",
        "timeout": "10",
    },
    # Waveshare 3.2" RPi LCD (B): physical pins 12/16/18 → BCM 18/23/24
    "gpio": {
        "key1_pin": "18",
        "key2_pin": "23",
        "key3_pin": "24",
        "key1_action": "next_stop",
        "key2_action": "next_page",
        "key3_action": "",
    },
}


def _city_urls(city: str) -> tuple[str, str]:
    """Return (base_url, stops_url) derived from *city* slug."""
    c = city.lower().strip()
    return (
        f"https://www.stops.lt/{c}/departures2.php",
        f"https://www.stops.lt/{c}/{c}/stops.txt",
    )


_APP_NAME = "lt-rtpi-display"


def _config_candidates() -> list[Path]:
    """Return config file paths in lookup order (first found wins)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    home = Path.home()
    paths = [Path("config.ini")]
    if xdg:
        paths.append(Path(xdg) / _APP_NAME / "config.ini")
    else:
        paths.append(home / ".config" / _APP_NAME / "config.ini")
    paths.append(Path(f"/etc/{_APP_NAME}/config.ini"))
    return paths


def load_config(path: Path | None = None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(_DEFAULTS)
    if path:
        cfg.read(path)
    else:
        for candidate in _config_candidates():
            if candidate.is_file():
                cfg.read(candidate)
                break
    # Derive API URLs from city when not explicitly set in config.
    if not cfg.get("api", "base_url") or not cfg.get("api", "stops_url"):
        base, stops = _city_urls(cfg.get("display", "city"))
        if not cfg.get("api", "base_url"):
            cfg.set("api", "base_url", base)
        if not cfg.get("api", "stops_url"):
            cfg.set("api", "stops_url", stops)
    return cfg


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def build_url(cfg: configparser.ConfigParser, stop_id: str) -> str:
    base = cfg.get("api", "base_url")
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
            # Kaunas inserts a numeric trip-ID at index 5;
            # destination shifts to index 6.
            dest_idx = 5
            if len(parts) >= 7 and parts[5].strip().isdigit():
                dest_idx = 6
            dep = Departure(
                type=VehicleType(parts[0].strip()),
                route=parts[1].strip(),
                direction=parts[2].strip(),
                dep_secs=int(parts[3].strip()),
                vehicle_id=parts[4].strip(),
                destination=parts[dest_idx].strip(),
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


def _parse_stops_raw(raw: bytes) -> list[StopInfo]:
    """Return (stop_id, name, direction, lat, lng) for every record in stops.txt.

    The header row is parsed to discover column positions (e.g. Kaunas has an
    extra SiriID column).  Name is inherited from the previous record when the
    field is absent or empty (stops.txt omits the Name field on duplicate-name
    consecutive records).
    """
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return []

    # Detect column layout from the header row.
    header = [c.strip().lower() for c in lines[0].split(";")]
    col = {}
    for name_key, aliases in (
        ("id", ("id",)),
        ("direction", ("direction",)),
        ("lat", ("lat",)),
        ("lng", ("lng",)),
        ("name", ("name",)),
    ):
        for i, h in enumerate(header):
            if h in aliases:
                col[name_key] = i
                break

    # Fallback to Vilnius-format indices when header detection fails.
    col.setdefault("id", 0)
    col.setdefault("direction", 1)
    col.setdefault("lat", 2)
    col.setdefault("lng", 3)
    col.setdefault("name", 5)

    def _get(parts: list[str], key: str) -> str:
        idx = col[key]
        return parts[idx].strip() if idx < len(parts) else ""

    result = []
    last_name = ""
    for line in lines[1:]:  # skip header
        parts = line.split(";")
        stop_id = _get(parts, "id")
        if not stop_id:
            continue
        name = _get(parts, "name")
        if not name:
            name = last_name
        else:
            last_name = name
        result.append(
            StopInfo(
                stop_id,
                name,
                _get(parts, "direction"),
                _get(parts, "lat"),
                _get(parts, "lng"),
            )
        )
    return result


def parse_stop_info(raw: bytes, stop_id: str) -> tuple[str | None, str | None]:
    """Return (name, direction) for stop_id from stops.txt, or (None, None)."""
    for stop in _parse_stops_raw(raw):
        if stop.stop_id == stop_id:
            return stop.name or None, stop.direction or None
    return None, None


def parse_all_stops(raw: bytes) -> list[StopInfo]:
    """Return displayable stops (those with valid coordinates)."""
    skip = ("", "0")
    return [s for s in _parse_stops_raw(raw) if s.lat not in skip or s.lng not in skip]


def fetch_stop_info(
    cfg: configparser.ConfigParser, stop_id: str, tz: datetime.tzinfo
) -> tuple[str | None, str | None]:
    url = build_stops_url(cfg, tz)
    timeout = cfg.getint("api", "timeout")
    raw = fetch_raw(url, timeout)
    return parse_stop_info(raw, stop_id)


def list_stops(cfg: configparser.ConfigParser) -> None:
    try:
        tz = ZoneInfo(cfg.get("display", "timezone"))
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")
    url = build_stops_url(cfg, tz)
    timeout = cfg.getint("api", "timeout")
    raw = fetch_raw(url, timeout)
    stops = parse_all_stops(raw)
    is_tty = sys.stdout.isatty()
    for stop in stops:
        display_name = stop.name
        if is_tty:
            lat_f = lng_f = 0.0
            try:
                lat_f = int(stop.lat) / 100_000
                lng_f = int(stop.lng) / 100_000
            except ValueError:
                pass
            if lat_f and lng_f:
                map_url = f"https://www.google.com/maps?q={lat_f},{lng_f}"
                display_name = f"\033]8;;{map_url}\033\\{stop.name}\033]8;;\033\\"
        print(f"{stop.stop_id:<6}  {display_name:<40}  {stop.direction}")  # noqa: T201


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
    # Post-midnight service day: API uses 24h+ notation (e.g. 25:30 = 91800s)
    # while seconds_since_midnight resets at 00:00
    if diff > 43200:
        diff -= 86400
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
        VehicleType.NIGHT: "N",
    }.get(t, "?")


def color_pair_for_type(t: VehicleType) -> ColorPair:
    return {
        VehicleType.TROL: ColorPair.TROL,
        VehicleType.BUS: ColorPair.BUS,
        VehicleType.EXPRESS: ColorPair.EXPRESS,
        VehicleType.NIGHT: ColorPair.NIGHT,
    }.get(t, ColorPair.BUS)


def color_pair_inv_for_type(t: VehicleType) -> ColorPair:
    return {
        VehicleType.TROL: ColorPair.TROL_INV,
        VehicleType.BUS: ColorPair.BUS_INV,
        VehicleType.EXPRESS: ColorPair.EXPRESS_INV,
        VehicleType.NIGHT: ColorPair.NIGHT_INV,
    }.get(t, ColorPair.BUS_INV)


# ---------------------------------------------------------------------------
# Background fetch thread
# ---------------------------------------------------------------------------


def fetch_and_update(cfg: configparser.ConfigParser, state: AppState) -> None:
    timeout = cfg.getint("api", "timeout")
    url = build_url(cfg, state.current_stop_id())
    try:
        raw = fetch_raw(url, timeout)
        _, deps = parse_response(raw)
        with state.lock:
            state.departures = deps
            state.last_updated = datetime.datetime.now(datetime.UTC)
            state.error_msg = None
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        with state.lock:
            state.error_msg = reason[:24]
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error_msg = type(exc).__name__[:24]


def _fetch_stop_metadata(
    cfg: configparser.ConfigParser, state: AppState, tz: datetime.tzinfo
) -> None:
    """Fetch and store stop name + direction for the current stop."""
    with contextlib.suppress(Exception):
        name, direction = fetch_stop_info(cfg, state.current_stop_id(), tz)
        with state.lock:
            state.stop_name = name
            state.stop_direction = direction


def background_worker(
    cfg: configparser.ConfigParser, state: AppState, tz: datetime.tzinfo
) -> None:
    interval = cfg.getint("display", "refresh_interval")
    _fetch_stop_metadata(cfg, state, tz)
    # First departure fetch immediately
    state.refreshing = True
    fetch_and_update(cfg, state)
    state.refreshing = False
    # Then loop — wake early on refresh_now (e.g. stop change)
    while not state.stop_event.is_set():
        triggered = state.refresh_now.wait(interval)
        if state.stop_event.is_set():
            break
        if triggered:
            state.refresh_now.clear()
            _fetch_stop_metadata(cfg, state, tz)
        state.refreshing = True
        fetch_and_update(cfg, state)
        state.refreshing = False


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _action_next_stop(state: AppState) -> None:
    with state.lock:
        state.stop_index = (state.stop_index + 1) % max(len(state.stop_ids), 1)
        state.departures = []
        state.stop_name = None
        state.stop_direction = None
        state.last_updated = None
        state.error_msg = None
        state.page_offset = 0
    state.refresh_now.set()


def _action_next_page(state: AppState) -> None:
    with state.lock:
        state.page_offset += 1


# ---------------------------------------------------------------------------
# GPIO button handling (Waveshare 3.2" RPi LCD B, pins 12/16/18 → BCM 18/23/24)
# ---------------------------------------------------------------------------


_GPIO_ROOT = Path("/sys/class/gpio")
_GPIO_DEBOUNCE_S = 0.3


def _gpio_chip_base() -> int:
    """Return the sysfs GPIO base offset for the bcm2835 gpiochip."""
    for chip in _GPIO_ROOT.glob("gpiochip*"):
        label_path = chip / "label"
        if label_path.exists() and "bcm2835" in label_path.read_text():
            return int((chip / "base").read_text().strip())
    return 0  # legacy kernels use base 0


def _gpio_set_pullup(bcm_pin: int) -> None:
    for cmd in (
        ["raspi-gpio", "set", str(bcm_pin), "pu"],
        ["pinctrl", "set", str(bcm_pin), "ip", "pu"],
    ):
        with contextlib.suppress(FileNotFoundError, OSError):
            if subprocess.run(cmd, capture_output=True, check=False).returncode == 0:  # noqa: S603
                return


_GPIO_ACTIONS: dict[str, Callable[[AppState], None]] = {
    "next_stop": _action_next_stop,
    "next_page": _action_next_page,
}


def _gpio_watch(pin_actions: dict[int, tuple[str, str]], state: AppState) -> None:
    """Background thread: epoll on sysfs value files, dispatch actions on press."""
    fds: dict[int, tuple[TextIOWrapper, str, str]] = {}
    for pin, (label, action) in pin_actions.items():
        with contextlib.suppress(OSError):
            fd = (_GPIO_ROOT / f"gpio{pin}" / "value").open()
            fd.read()  # initial read to arm the edge interrupt
            fds[fd.fileno()] = (fd, label, action)
    if not fds or not hasattr(select, "epoll"):
        return
    ep = select.epoll()  # type: ignore[attr-defined]
    for fileno in fds:
        ep.register(fileno, select.EPOLLPRI | select.EPOLLERR)  # type: ignore[attr-defined]
    last_press: dict[int, float] = {}
    try:
        while not state.stop_event.is_set():
            for fileno, _ in ep.poll(timeout=1.0):
                fd, label, action = fds[fileno]
                fd.seek(0)
                val = fd.read().strip()
                now = time.monotonic()
                if val == "0" and now - last_press.get(fileno, 0) >= _GPIO_DEBOUNCE_S:
                    last_press[fileno] = now
                    handler = _GPIO_ACTIONS.get(action)
                    if handler:
                        handler(state)
    finally:
        ep.close()
        for fd, _, _ in fds.values():
            fd.close()


def setup_gpio(cfg: configparser.ConfigParser, state: AppState) -> list[int]:
    """Export sysfs GPIO pins for K1/K2/K3 and start edge-detect thread."""
    if not _GPIO_ROOT.is_dir() or not hasattr(select, "epoll"):
        return []
    keys = {
        "K1": (cfg.getint("gpio", "key1_pin"), cfg.get("gpio", "key1_action")),
        "K2": (cfg.getint("gpio", "key2_pin"), cfg.get("gpio", "key2_action")),
        "K3": (cfg.getint("gpio", "key3_pin"), cfg.get("gpio", "key3_action")),
    }
    base = _gpio_chip_base()
    exported: dict[int, tuple[str, str]] = {}
    for label, (bcm, action) in keys.items():
        sysfs_pin = bcm + base
        _gpio_set_pullup(bcm)
        try:
            pin_dir = _GPIO_ROOT / f"gpio{sysfs_pin}"
            if not pin_dir.exists():
                (_GPIO_ROOT / "export").write_text(str(sysfs_pin))
                time.sleep(0.1)
            if (pin_dir / "direction").read_text().strip() != "in":
                (pin_dir / "direction").write_text("in")
            if (pin_dir / "edge").read_text().strip() != "falling":
                (pin_dir / "edge").write_text("falling")
            exported[sysfs_pin] = (label, action)
        except OSError:
            pin_dir = _GPIO_ROOT / f"gpio{sysfs_pin}"
            if pin_dir.exists() and (pin_dir / "edge").read_text().strip() == "falling":
                exported[sysfs_pin] = (label, action)
    if exported:
        threading.Thread(
            target=_gpio_watch, args=(exported, state), daemon=True
        ).start()
    return list(exported)


def teardown_gpio(pins: list[int]) -> None:
    for pin in pins:
        with contextlib.suppress(OSError):
            (_GPIO_ROOT / "unexport").write_text(str(pin))


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
        curses.init_color(ColorSlot.NIGHT, *_rgb(48, 48, 48))  # dark gray
        trol_color: int = ColorSlot.TROL
        bus_color: int = ColorSlot.BUS
        express_color: int = ColorSlot.EXPRESS
        night_color: int = ColorSlot.NIGHT
    else:
        trol_color = curses.COLOR_RED
        bus_color = curses.COLOR_BLUE
        express_color = curses.COLOR_GREEN
        night_color = curses.COLOR_BLACK

    curses.init_pair(ColorPair.TROL, trol_color, -1)
    curses.init_pair(ColorPair.BUS, bus_color, -1)
    curses.init_pair(ColorPair.EXPRESS, express_color, -1)
    curses.init_pair(ColorPair.TROL_INV, curses.COLOR_WHITE, trol_color)
    curses.init_pair(ColorPair.BUS_INV, curses.COLOR_WHITE, bus_color)
    curses.init_pair(ColorPair.EXPRESS_INV, curses.COLOR_WHITE, express_color)
    curses.init_pair(ColorPair.NIGHT, night_color, -1)
    curses.init_pair(ColorPair.NIGHT_INV, curses.COLOR_WHITE, night_color)
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


def draw_status_bar(
    win: curses.window,
    rows: int,
    cols: int,
    refreshing: bool,  # noqa: FBT001
    last_updated: datetime.datetime | None,
    page_info: str,
    tz: datetime.tzinfo,
    dim: int,
) -> None:
    if refreshing:
        left_status = " Refreshing..."
    elif last_updated:
        left_status = f" {last_updated.astimezone(tz).strftime('%H:%M:%S')}"
    else:
        left_status = " Connecting..."
    safe_addstr(win, rows - 1, 0, left_status, dim)
    # Right side: keyboard hints (added if space permits)
    right_parts: list[str] = []
    hints = ["[n]ext"]
    if page_info:
        hints.insert(0, f"[j] page {page_info}")
    for hint in hints:
        candidate = " ".join([*right_parts, hint, "[q]uit"])
        if len(left_status) + len(candidate) + 3 < cols:
            right_parts.append(hint)
    right_parts.append("[q]uit")
    right_text = " ".join(right_parts) + " "
    safe_addstr(win, rows - 1, cols - len(right_text) - 1, right_text, dim)


def draw_screen(
    stdscr: curses.window,
    *,
    stop_id: str,
    stop_name: str | None,
    stop_direction: str | None,
    departures: list[Departure],
    last_updated: datetime.datetime | None,
    error_msg: str | None,
    refreshing: bool,
    page_offset: int,
    tz: datetime.tzinfo,
) -> int:
    """Draw the full screen. Returns the clamped page_offset actually used."""
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()

    if rows < 5 or cols < 20:
        safe_addstr(stdscr, 0, 0, "Terminal too small", curses.A_BOLD)
        return 0

    bold = curses.color_pair(ColorPair.HEADER) | curses.A_BOLD
    dim = curses.color_pair(ColorPair.SEP)

    # --- Row 0: title (+ error indicator if any) ---
    base_name = f" {stop_name}" if stop_name else f" Stop {stop_id or '?'}"
    safe_addstr(stdscr, 0, 0, base_name, bold)
    if stop_direction:
        plain = curses.color_pair(ColorPair.HEADER)
        suffix = f" | {stop_direction}"
        safe_addstr(stdscr, 0, len(base_name), suffix, plain)
    if error_msg:
        err_text = f"ERR:{error_msg}"
        dir_len = len(f" | {stop_direction}") if stop_direction else 0
        title_end = len(base_name) + dir_len
        err_col = max(title_end + 1, cols - len(err_text) - 1)
        safe_addstr(
            stdscr,
            0,
            err_col,
            err_text,
            curses.color_pair(ColorPair.ERROR) | curses.A_BOLD,
        )

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

    # --- Rows 4..rows-2: departure rows (with paging) ---
    per_page = rows - 5  # rows 4 to rows-2 inclusive
    total = len(departures)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = page_offset % total_pages if total_pages > 0 else 0
    start = page * per_page
    visible = departures[start : start + per_page]

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
        route_str = dep.route[:route_width].upper()
        route_padded = route_str.rjust(route_width - 1).ljust(route_width)
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
    page_info = f"{page + 1}/{total_pages}" if total_pages > 1 else ""
    draw_status_bar(
        stdscr,
        rows,
        cols,
        refreshing,
        last_updated,
        page_info,
        tz,
        dim,
    )
    return page


def _main_handle_key(key: int, state: AppState) -> None:
    if key == ord("n"):
        _action_next_stop(state)
    elif key == ord("j"):
        _action_next_page(state)
    elif key == ord("k"):
        with state.lock:
            state.page_offset = max(0, state.page_offset - 1)


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

    raw_ids = cfg.get("display", "stop_ids").split(",")
    stop_ids = [s.strip() for s in raw_ids if s.strip()]
    try:
        tz = ZoneInfo(cfg.get("display", "timezone"))
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")

    state = AppState(stop_ids=stop_ids)

    # Handle SIGTERM (systemd stop) gracefully
    def _sigterm(_signum: int, _frame: object) -> None:
        state.stop_event.set()

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    # Set up physical buttons (no-op if RPi.GPIO not available)
    gpio_pins = setup_gpio(cfg, state)

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
        _main_handle_key(key, state)

        # Snapshot state under lock
        with state.lock:
            departures = list(state.departures)
            sid = state.current_stop_id()
            sname = state.stop_name
            sdirection = state.stop_direction
            updated = state.last_updated
            error = state.error_msg
            pg_offset = state.page_offset
        refreshing = state.refreshing

        clamped = draw_screen(
            stdscr,
            stop_id=sid,
            stop_name=sname,
            stop_direction=sdirection,
            departures=departures,
            last_updated=updated,
            error_msg=error,
            refreshing=refreshing,
            page_offset=pg_offset,
            tz=tz,
        )
        # Keep page_offset in sync (wraps around via modulo)
        if clamped != pg_offset:
            with state.lock:
                state.page_offset = clamped
        stdscr.refresh()

    # Clean shutdown
    state.stop_event.set()
    worker.join(timeout=2)
    teardown_gpio(gpio_pins)
    curses.endwin()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    parser = argparse.ArgumentParser(description="RTPI departure board")
    parser.add_argument("stop_id", nargs="?", help="Stop ID (overrides config)")
    parser.add_argument("-c", "--config", metavar="FILE", help="Path to config file")
    parser.add_argument("--city", help="City slug (e.g. vilnius, klaipeda, panevezys)")
    parser.add_argument(
        "-l", "--list", action="store_true", help="List all stops and exit"
    )
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else None
    cfg = load_config(config_path)

    if args.city:
        cfg.set("display", "city", args.city)
        # Re-derive API URLs for the new city.
        base, stops = _city_urls(args.city)
        cfg.set("api", "base_url", base)
        cfg.set("api", "stops_url", stops)

    if args.list:
        list_stops(cfg)
        return

    if args.city and not args.stop_id:
        parser.error("--city requires a stop ID (stop IDs differ between cities)")

    if args.stop_id:
        cfg.set("display", "stop_ids", args.stop_id)

    with contextlib.suppress(KeyboardInterrupt):
        curses.wrapper(main, cfg)


if __name__ == "__main__":
    run()
