# Linked Lights

Two Raspberry Pi Pico W boards keeping their SK6812 RGBW LED strips in perfect sync over MQTT. Touch one lamp and both shift colour together. Leave them alone and they slowly drift — barely noticeably — keeping themselves alive. Control everything wirelessly from a web app that works on any phone or browser.

---

## Features

- **Sync** — tap one lamp, both change colour simultaneously via MQTT
- **LED groups** — strip divided into independent colour zones, sizes randomise on every tap
- **Organic feel** — each group breathes independently, colours drift subtly over time
- **Boss / follower** — one board publishes state every 60 s; the follower slow-fades to match
- **Animation modes** — Rainbow, Wave, Candle, and a fully configurable Custom builder
- **Sunrise alarm** — ramps brightness from zero over a set duration, configurable per weekday
- **Web app** — full wireless control; scenes and settings sync across all connected clients via MQTT retain
- **OTA updates** — boards pull latest firmware from GitHub every day at 5 pm UTC; no reflashing
- **Multi-network WiFi** — tries known networks in order; new networks can be added from the web app
- **WebREPL** — live REPL and file upload over WiFi once the board is running

---

## Hardware

### What you need (per lamp)

| Part | Notes |
|---|---|
| Raspberry Pi Pico W | The W variant — needs WiFi |
| SK6812 RGBW LED strip | GRBW byte order; any length |
| Capacitive touch pad | Bare copper or foil, 1 MΩ pull-down to GND |
| 5 V power supply | Power the strip directly for more than ~10 LEDs |
| 300–470 Ω resistor | Series on the DIN data line — prevents ringing |

### Wiring

```
Pico W                    SK6812 strip
GPIO LED_PIN ──[330Ω]──── DIN ────────────── DOUT
3.3 V or 5 V  ───────────── VCC
GND           ───────────── GND

Pico W                    Touch sensor
GPIO TOUCH_PIN ──┬──────── copper pad / foil
                 └─[1MΩ]── GND
```

> **Data direction matters.** Data flows DIN → DOUT through each LED in one direction only.
> Both boards must connect their data wire to a **DIN** pad, never DOUT.

#### Board B — far-end connector

If board B connects via a connector at the far end of a shared strip, that end is DOUT and won't work. Either run a fresh data wire back to the DIN end, or set `REVERSE_LEDS = True` in `config.py` to flip the group order in software.

---

## First-Time Setup

### 1 — Flash MicroPython

Download the Pico W UF2 from https://micropython.org/download/RPI_PICO_W/, hold BOOTSEL, drag it on.

### 2 — Install umqtt

In Thonny's shell (or `mpremote`):

```python
import mip
mip.install("umqtt.simple")
```

### 3 — Enable WebREPL (once per board)

```python
import webrepl_setup
```

Set the password to match `WEBREPL_PASSWORD` in `config.py` (default: `linked1`). This persists across reboots.

### 4 — Upload files

```bash
mpremote connect /dev/ttyACM0 cp boot.py main.py config.py colour.py sk6812.py touch.py :
```

Or use Thonny: File → Upload to /.

### 5 — Edit config.py on each board

```python
# ── Identity ────────────────────────────────────────────────
BOARD_ID = "board_a"          # "board_b" on the second Pico
BOSS     = True               # True on board_a only

# ── WiFi ────────────────────────────────────────────────────
WIFI_NETWORKS = [
    ("home_wifi",   "password"),
    ("phone_hotspot", "password"),
]

# ── MQTT ────────────────────────────────────────────────────
MQTT_BROKER       = "broker.hivemq.com"   # or your own Mosquitto IP
MQTT_PORT         = 1883
MQTT_TOPIC_PREFIX = "picolight_yourname"  # must match on both boards

# ── LED strip ───────────────────────────────────────────────
LED_PIN    = 5    # GPIO connected to DIN
NUM_LEDS   = 10   # number of LEDs
REVERSE_LEDS = False   # True on board_b if connected at far end

# ── Touch ───────────────────────────────────────────────────
TOUCH_PINS = [12]   # list of GPIO pins with sensors
```

### 6 — MQTT broker

**Free public broker** (no setup):
```python
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT   = 1883
```
Change `MQTT_TOPIC_PREFIX` to something unique — the public broker is shared with strangers.

**Self-hosted Mosquitto** (more reliable):
```bash
sudo apt install mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
```
Set `MQTT_BROKER` to your machine's LAN IP or domain.

---

## OTA Updates

`boot.py` runs before `main.py` on every boot. It connects to WiFi and pulls the latest `main.py`, `colour.py`, `sk6812.py`, and `touch.py` from the GitHub repo. If a file hasn't changed it's skipped; if WiFi isn't available the board boots with whatever is already on it.

A daily automatic reboot fires at **17:00 UTC** (configurable via `OTA_HOUR_UTC` in `main.py`) so boards stay up to date without intervention. Before rebooting, the board saves whether the lamp was on or off and restores that state after the update.

`config.py` and `boot.py` are **never touched by OTA** — board-specific settings are always preserved.

To push a change to both boards: commit and push to the `master` branch. Both boards will pick it up at 17:00 UTC, or immediately on their next manual reboot.

---

## Web App

Hosted on GitHub Pages. Open it on any phone or browser — settings and scenes sync across all connected clients via MQTT retained topics, so there's no per-device setup.

### Controls

| Control | What it does |
|---|---|
| **Tap** | Random colour change across all groups — same as physically touching the lamp |
| **Power** | Toggle both lamps on / off with a slow fade |
| **Reset** | Return all groups to pure warm white |
| **Auto Drift** | Enable / disable the idle colour drift |
| **Brightness** | Global brightness cap (0–100%) |
| **Fade Speed** | How long colour transitions take (steps slider) |
| **Drift Every** | How many seconds between autonomous drift events |

### Google Home (on/off only)

The firmware now exposes a simple MQTT on/off interface:

- Command topic: `YOUR_PREFIX/power/set` (`ON` / `OFF`)
- State topic: `YOUR_PREFIX/power/state` (`ON` / `OFF`, retained)

It also publishes Home Assistant MQTT discovery at:

- `homeassistant/switch/YOUR_PREFIX_light/config` (retained)

So if you use Home Assistant with Google Home, the lamps can be controlled by voice as an on/off switch only.

### Strip preview

The top card shows the LED strip in real time. Click the divider cells below the strip to set group boundaries — where one colour zone ends and the next begins.

### Groups

Each group has a **colour handle** on the shared palette bar — drag it left/right to pick a hue. All groups are visible at once so it's easy to space them out and avoid duplicates. Each group also has an independent **Warm White** slider.

### Animation modes

| Mode | Description |
|---|---|
| **Off** | Static colours with breathing |
| **Rainbow** | Hue cycles continuously across all groups |
| **Wave** | Brightness ripples through the groups |
| **Candle** | Random flicker around the current colours |
| **Custom** | Configurable — see below |

#### Custom animation builder

Click **+ Build Custom Animation** to expand the builder panel:

- **Pattern** — *Sweep*: hue moves from start to end colour and back. *Bounce*: same but colour oscillates. *Pulse*: groups fade in and out at the midpoint colour. *Strobe*: rapid flash at the start colour.
- **Colour Range** — two draggable handles on the palette bar set the start and end hue.
- **Groups** — *Offset* means each group animates slightly out of phase with the others (more organic). *In Sync* means all groups move together.
- **Apply** — sends the animation to both boards immediately.
- **Save** — stores it as a named scene for one-tap recall later.

### Sunrise alarm

Set up to any number of alarms — each with a wake time, target brightness, fade duration (5–120 min), and weekday selection. Alarms are published to a retained MQTT topic so boards receive them even after a reboot without the web app being open.

### Scenes

Save and load complete lamp states including colours, group layout, animation mode, and settings. Scenes are stored in a retained MQTT topic and shared across all clients automatically.

### Presets

One-tap built-in scenes: Rainbow, Candle, Wave, Sunset, Night, Warm White.

### Settings

- MQTT broker URL, topic prefix, LED count, group count
- Board A and Board B identifiers and display names (shown in the status pills)
- Add a WiFi network — sends the credentials to both boards over MQTT; they save it and use it on next boot

---

## Status Pills

The two pills at the top right (default: **A** and **B**) show whether each board is reachable. They go green when a heartbeat or any message has been received from that board within the last 90 seconds. Names and board IDs are configurable in Settings.

---

## How It Works

```
Physical tap on lamp A
       │
       ▼
TouchManager detects event
       │
       ▼
ColourEngine.impulse()
  • re-partitions LED groups randomly
  • assigns each group a random palette position (0.0–1.0)
  • assigns each group a random warm-white level (60–100%)
  • starts per-group crossfades
       │
       ▼
publish_event() → MQTT broker → lamp B subscribes
       │
       ▼
ColourEngine.force_colour(groups)
  • applies the exact same positions and group sizes
  • uses a slow fade if this is a boss→follower sync
```

**Palette position** is a float 0.0–1.0 interpolating through `TINT_PALETTE`. At 0.0 the W channel is full and RGB is off (pure warm white). At 1.0 the palette colour is fully saturated and W fades to zero.

**Boss / follower sync** — every 60 seconds the boss publishes its full state with a `sync: true` flag. The follower applies it with a very slow fade (300 steps, ~5 s) so the re-alignment is invisible.

**Drift** — every `IDLE_DRIFT_INTERVAL_S` seconds, each group nudges its hue by 1–5% of the palette, tweaks its warm-white level by up to ±8%, and has a 25% chance of shifting a group boundary by ±1 LED. The boss publishes after drifting so both boards stay together.

---

## Colour Customisation

All colour configuration lives in `config.py`:

```python
# Pure warm white (W channel only at startup)
BASE_WARM_WHITE = (0, 0, 0, 200)

# How far a tap can jump along the palette (0.0–1.0)
HUE_SHIFT_MIN = 0.20
HUE_SHIFT_MAX = 0.50

# Breathing
BREATHE_SPEED = 0.0008    # radians/ms — higher = faster
BREATHE_DEPTH = 0.08      # amplitude — 0 = no breathing

# Colour groups
NUM_GROUPS    = 3
GROUP_MIN_LEDS = 1
GROUP_MAX_LEDS = 8

# Drift
IDLE_DRIFT_INTERVAL_S = 65
```

`TINT_PALETTE` is a list of `(R, G, B)` tuples — add, remove, or reorder freely. The engine interpolates smoothly between adjacent entries.

---

## Touch Calibration

If touches are missed or false-triggering:

1. Watch `[touch]` output at boot to see baseline values
2. Adjust in `config.py`:
   - `TOUCH_THRESHOLD` — lower = more sensitive (try 1–10)
   - `HOLD_TIME_MS` — how long a press must last to count as hold / power toggle (default 3000 ms)
3. For long wires, try a smaller pull-down (470 kΩ instead of 1 MΩ)

---

## Wireless File Editing (WebREPL)

Once a board is on WiFi, its IP is printed at boot:
```
[wifi] connected to home_wifi — IP 192.168.1.42
[webrepl] started — connect at ws://192.168.1.42:8266
```

Open **http://micropython.org/webrepl/**, enter `ws://192.168.x.x:8266` and the `WEBREPL_PASSWORD`. Drag `.py` files onto the page to upload them without USB.

Assign the board a static IP via your router's DHCP reservation (by MAC address) so the address never changes.

---

## Files

| File | Updated by OTA | Purpose |
|---|---|---|
| `config.py` | Never | Per-board settings — WiFi, MQTT, LED pin, palette |
| `boot.py` | Never | OTA updater — runs before main.py on every boot |
| `main.py` | Yes | Main firmware loop — WiFi, MQTT, touch, sync, alarms |
| `colour.py` | Yes | Colour engine — groups, fades, breathing, drift, animations |
| `sk6812.py` | Yes | PIO-based SK6812 RGBW LED driver |
| `touch.py` | Yes | Capacitive touch detection |
| `hardware_test.py` | — | Standalone wiring test, no WiFi needed |
| `web_app/index.html` | — | Web app source (deployed via GitHub Pages) |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Board B LEDs don't light | Connected to DOUT not DIN | Run data wire to DIN end |
| Wrong colours | Byte order mismatch | SK6812 uses GRBW — confirmed in `sk6812.py` |
| Groups reversed on board B | Far-end connector | Set `REVERSE_LEDS = True` in `config.py` |
| Touch never fires | Threshold too high | Lower `TOUCH_THRESHOLD` |
| Touch fires constantly | Missing pull-down | Add 1 MΩ between GPIO and GND |
| Boards not syncing | Different topic prefix | `MQTT_TOPIC_PREFIX` must be identical on both |
| `[mqtt] check_msg error: -1` | Broker dropped idle connection | Already handled — board reconnects automatically |
| WebREPL won't connect | `webrepl_setup` not run | Run `import webrepl_setup` once via USB |
| OTA not updating | `boot.py` not on board | Flash `boot.py` manually once via USB |
| Sunrise alarm not firing | NTP not synced | Board must have WiFi at boot; check `[ntp]` log output |
