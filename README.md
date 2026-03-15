# pi-rtpi-display

Real-Time Passenger Information (RTPI) departure board for Raspberry Pi, designed to run in a TTY on a [Waveshare 3.2" display](https://www.waveshare.com/3.2inch-rpi-lcd-b.htm) running [DietPi](https://dietpi.com/). Works exactly like dietpi-cloudshell — no desktop, no browser, no GPU.

```
 Stop 0410           Updated 15:29:05
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
                             [q] quit
```

## Requirements

- Raspberry Pi (tested on Pi 1)
- Python 3 (`sudo apt install python3`) — **no pip installs needed**
- Network access to `stops.lt`

## Quick start

```bash
git clone https://github.com/youruser/pi-rtpi-display
cd pi-rtpi-display
python3 rtpi.py
```

Press `q`, `Q`, or `Esc` to exit.

## Configuration

Edit `config.ini`:

```ini
[display]
stop_id = 0410          # Stop ID from stops.lt
refresh_interval = 30   # Seconds between API fetches
max_departures = 20     # Max rows to display

[api]
base_url = https://www.stops.lt/vilnius/departures2.php
timeout = 10            # HTTP request timeout in seconds
```

Find your stop ID on [stops.lt](https://www.stops.lt/vilnius/) — it appears in the URL when you select a stop.

## Autostart on boot (systemd)

Copy the files to the Pi and install the service:

```bash
# On your development machine
scp rtpi.py config.ini rtpi.service dietpi@<pi-ip>:/home/dietpi/pi-rtpi-display/

# On the Pi
sudo cp /home/dietpi/pi-rtpi-display/rtpi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable rtpi.service
sudo systemctl start rtpi.service
```

Check status:

```bash
sudo systemctl status rtpi.service
```

The service binds to `/dev/tty1` (the physical display) with `TERM=linux`, matching how dietpi-cloudshell works on the Waveshare framebuffer console.

## Display

| Column | Values |
|--------|--------|
| T | **T** = trolleybus (cyan), **B** = bus (green), **E** = express bus (yellow) |
| Rte | Route number |
| Destination | Final stop name |
| Due | `Due` = imminent (red), `Xm` = minutes, `HH:MM` = scheduled time |

If the API is unreachable, the last fetched data remains visible with an error indicator in the top-right corner.

## Data source

Departure data is fetched from the [stops.lt](https://www.stops.lt) public API (Vilnius, Lithuania).
