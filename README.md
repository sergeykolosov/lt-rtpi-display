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
git clone https://github.com/youruser/lt-rtpi-display
cd lt-rtpi-display
python3 rtpi.py
```

## Configuration

Edit `config.ini`:

```ini
[display]
city = vilnius           # Or kaunas/klaipeda/panevezys/alytus/druskininkai
stop_ids = 0410, 0409    # Comma-separated stop IDs (at least one)
```

Find your stop ID on [stops.lt](https://www.stops.lt/vilnius/) — it appears in the URL when you select a stop. Use `python3 rtpi.py --list` to print all known stops, or `python3 rtpi.py --city kaunas --list` for another city.

For the full list of configurable options see bundled [config.ini](./config.ini).

## CLI

```bash
python3 rtpi.py [stop_id]              # Override configured stop(s)
python3 rtpi.py --city klaipeda 0701   # Use a different city + specific stop
python3 rtpi.py --list                 # List all stops and exit
python3 rtpi.py --city kaunas -l       # List stops for another city
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
# On your development machine
scp rtpi.py config.ini rtpi.service user@<pi-ip>:~/lt-rtpi-display/

# On the Pi
sudo cp ~/lt-rtpi-display/rtpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rtpi.service
```

The included `rtpi.service` binds to `/dev/tty1` with `TERM=linux`, matching how dietpi-cloudshell works on the framebuffer console.

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
