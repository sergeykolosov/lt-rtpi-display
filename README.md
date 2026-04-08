# lt-rtpi-display

A lightweight terminal departure board for public transport in Vilnius and other Lithuanian cities. Fetches real-time data from [stops.lt](https://www.stops.lt) API and renders a curses TUI — no browser, no desktop, no dependencies beyond Python 3.11+ stdlib. For a dedicated kiosk on a small screen, see [Raspberry Pi kiosk setup](#raspberry-pi-kiosk-setup).

```text
 MO muziejus | link centro
─────────────────────────────────────
 T  Rte  Destination              Due
─────────────────────────────────────
 T    2  Saulėtekis               Due
 B   53  Fabijoniškės              2m
 E   1g  Santariškės               3m
 B   10  Fabijoniškės              7m
 T    2  Saulėtekis               10m
 T    7  Perkūnkiemis             12m
 B   88  Europos aikštė           14m
─────────────────────────────────────
 22:41:08  [j] page 1/3 [n]ext [q]uit
```

Supported cities: Vilnius, Kaunas, Klaipėda, Panevėžys, Alytus, Druskininkai.

## Quick start

```bash
# Run directly (no install needed):
uvx lt-rtpi-display --city vilnius 0103

# Or install permanently:
uv tool install lt-rtpi-display
lt-rtpi-display --city vilnius 0103

# Or from source:
git clone https://github.com/sergeykolosov/lt-rtpi-display
cd lt-rtpi-display
uv run lt-rtpi-display --city vilnius 0103
```

## Configuration

For permanent setup, copy the example config and edit:

```bash
mkdir -p ~/.config/lt-rtpi-display
cp config.example.ini ~/.config/lt-rtpi-display/config.ini
```

```ini
[display]
city = vilnius           # Or kaunas/klaipeda/panevezys/alytus/druskininkai
stop_ids = 0410, 0409    # Comma-separated stop IDs (at least one)
```

Config lookup order (first found wins):

1. `-c /path/to/config.ini` (explicit)
2. `./config.ini` (current directory)
3. `$XDG_CONFIG_HOME/lt-rtpi-display/config.ini` (usually `~/.config/...`)
4. `/etc/lt-rtpi-display/config.ini` (system-wide, good for kiosk)
5. Built-in defaults

Find your stop ID on [stops.lt](https://www.stops.lt/vilnius/) — it appears in the URL when you select a stop. Use `lt-rtpi-display --list` to print all known stops, or `lt-rtpi-display --city kaunas --list` for another city.

For the full list of configurable options see [config.example.ini](./config.example.ini).

## CLI

```bash
lt-rtpi-display [stop_id]                   # Override configured stop
lt-rtpi-display -c /path/config.ini         # Use a specific config file
lt-rtpi-display --list                      # List all stops and exit

# examples:
lt-rtpi-display "0103, 0104"                # Run against multiple stops (switch with [n])
lt-rtpi-display --city klaipeda 0901        # Use a different city + specific stop
lt-rtpi-display --city kaunas --list        # List stops for another city
```

## Display

| Column | Content |
|--------|---------|
| T | **T** = trolleybus, **B** = bus, **E** = express bus, **N** = night bus |
| Rte | Route number (colored badge) |
| Destination | Final stop name |
| Due | `Due` (red) = imminent, `Xm` = minutes, `HH:MM` = scheduled |

Uses brand colors matching Vilnius public transport: red for trolleybuses, blue for buses, green for express, etc. Exact RGB when the terminal supports custom colors, nearest standard color otherwise.

## Keyboard controls

| Key | Action |
|-----|--------|
| `j` | Next page |
| `k` | Previous page |
| `n` | Next stop |
| `q` / `Esc` | Quit |

---

## Raspberry Pi kiosk setup

This is the original use case: a dedicated departure board running on a Raspberry Pi 1 with a [Waveshare 3.2" RPi LCD (B)](https://www.waveshare.com/3.2inch-rpi-lcd-b.htm) on [DietPi](https://dietpi.com/) — no desktop, just the framebuffer console on `/dev/tty1`.

### Requirements

- Raspberry Pi (tested on Pi 1)
- Python 3.11+ (`sudo apt install python3`)
- Network access to `stops.lt`

### Autostart (systemd)

```bash
# On the Pi — install from PyPI:
python -m pip install lt-rtpi-display

# Set up config:
mkdir -p ~/.config/lt-rtpi-display
cp config.example.ini ~/.config/lt-rtpi-display/config.ini  # Edit to set your stop_ids

# Or clone the repo and install from source:
# git clone https://github.com/sergeykolosov/lt-rtpi-display ~/lt-rtpi-display
# python -m pip install ~/lt-rtpi-display

# Install and enable the service:
sudo cp lt-rtpi-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lt-rtpi-display.service
```

The service runs the installed `lt-rtpi-display` console script. It binds to `/dev/tty1` with `TERM=linux`, matching how dietpi-cloudshell works on the framebuffer console.

### Physical buttons

The Waveshare 3.2" LCD (B) has three GPIO buttons (K1/K2/K3). Configure actions in `config.ini`:

```ini
[gpio]
key1_pin = 18
key2_pin = 23
key3_pin = 24
key1_action = next_stop
key2_action = next_page
key3_action =
```

Available actions: `next_stop`, `next_page`.

The user (e.g. `dietpi`) must be in the `gpio` group: `sudo usermod -aG gpio dietpi`.

---

## Development

Requires [uv](https://docs.astral.sh/uv/):

```bash
uv sync                          # Install dev dependencies + editable package
uv run lt-rtpi-display --help    # Run via console script
uv run python -m lt_rtpi_display # Run via __main__
uv run -- ruff check             # Lint
uv run -- ruff format            # Format
uv run -- mypy                   # Type check
```
