# Linked Friend Lights

Two Raspberry Pi Pico W boards keeping their SK6812 RGBW LED strips in sync over MQTT. Touch one lamp and both shift colour together. Control everything from a web app that works on any phone or browser.

---

## Overview

```
[Web UI] ──MQTT (WebSocket)── [board_a / BOSS] ──sync── [board_b / follower]
                                SK6812 strip               SK6812 strip
```

One board (`board_a`, display name **FF**) is the BOSS; the other (`board_b`, **LS**) is the follower. Both subscribe to the same MQTT topic and keep their strips in sync. The web UI publishes JSON commands to the same topic and reflects live state from both boards.

---

## Hardware

| Part | Notes |
|------|-------|
| Raspberry Pi Pico W (×2) | The W variant — needs WiFi |
| SK6812 RGBW LED strip | 10 LEDs per board, GRBW byte order |
| Data pin | GPIO 5 |
| 300–470 Ω resistor | Series on DIN — prevents ringing |
| 5 V power supply | USB or direct to strip |

### Wiring

```
Pico W GPIO 5 ──[330Ω]──── DIN ────────────── DOUT
5 V / 3.3 V   ─────────── VCC
GND           ─────────── GND

Pico W TOUCH_PIN ──┬──── copper pad / foil
                   └─[1MΩ]── GND
```

> Data flows DIN → DOUT; connect the board's data wire to the **DIN** end of the strip.

If board B connects via the far end (DOUT side), set `REVERSE_LEDS = True` in `config.py` to flip group order in software.

---

## Firmware

### Files

| File | OTA-updated | Purpose |
|------|-------------|---------|
| `boot.py` | Never | OTA bootstrap — downloads firmware from GitHub on every boot |
| `main.py` | Yes | Main loop: WiFi, MQTT, touch, alarms, BOSS sync |
| `colour.py` | Yes | Colour engine: groups, fades, breathing, drift |
| `sk6812.py` | Yes | PIO-based SK6812 RGBW driver |
| `touch.py` | Yes | Capacitive touch detection |
| `config.py` | Never | Per-board constants (never overwritten by OTA) |

### OTA Updates

`boot.py` runs before `main.py` on every boot. It connects to WiFi and downloads the latest `main.py`, `colour.py`, `sk6812.py`, and `touch.py` from the `master` branch of this repo. `config.py` and `boot.py` are never touched. If WiFi is unavailable the board boots from whatever is already on flash.

To update both boards: commit and push to `master`, then click **↺ Reboot Both Boards** in the Settings panel.

Updates are **all-or-nothing**: every file is downloaded and compile-checked before any is installed, so a mid-update network drop can't leave a new `main.py` running against an old `colour.py`.

### Crash-loop rollback

The compile gate rejects firmware that doesn't parse, but valid code can still fail at runtime — a stray function-level `import` once shadowed a module-level one and both boards boot-looped, unreachable until a human pushed a fix. So the boards now keep a way back:

1. `boot.py` increments a counter in `bootfail.txt` on every boot. A **power-on** reset doesn't count — only soft and watchdog resets, which is what a crash loop produces.
2. Once `main.py` has run for a minute it calls `mark_stable()`: the counter is cleared and the running files are promoted to `.ok` backups. Backups are only rewritten when they actually differ, so this costs no flash in steady state.
3. After **3** boots that never reach stability, `boot.py` restores the `.ok` set and skips the update for that one boot — otherwise it would immediately reinstall the firmware that was failing. The lamp comes back on its own in about 15 seconds.
4. The board reports `rolled_back: true` in its heartbeat until it is stable again, so a rejected push is visible rather than silent.

No record is kept of the rejected version: the next scheduled reboot re-downloads `master`, so a pushed fix is picked up with no stale state to clear, and a few quick power cycles can't pin a board to old firmware.

> `boot.py` is **never** updated over the air — a broken bootloader would need USB to recover. Copy it to each board manually once to get this feature (`mpremote connect /dev/ttyACM0 cp boot.py :`).

### Configuration (`config.py`)

```python
BOARD_ID   = "board_a"    # "board_b" on the second Pico
BOSS       = True         # True on board_a only

NUM_LEDS       = 10
LED_PIN        = 5
REVERSE_LEDS   = False

NUM_GROUPS     = 3
GROUP_MIN_LEDS = 1
GROUP_MAX_LEDS = 8

LED_BRIGHTNESS = 0.6
FADE_STEPS     = 60       # steps per colour transition (~1 s at 60 Hz)
BREATHE_SPEED  = 0.0008   # radians/ms
BREATHE_DEPTH  = 0.04     # ±4% brightness oscillation

IDLE_DRIFT_INTERVAL_S = 45

MQTT_BROKER        = "broker.hivemq.com"
MQTT_PORT          = 1883
MQTT_TOPIC_PREFIX  = "picolight_lf26"

WIFI_NETWORKS = [
    ("YourSSID", "YourPassword"),
]
```

### MQTT

- **Broker:** `broker.hivemq.com:1883` (TCP on device) / `:8884` WSS (web)
- **Events topic:** `picolight_lf26/events`
- **Alarms topic:** `picolight_lf26/alarms` (retained, subscribed by boards)
- **Status topic:** `picolight_lf26/status/<board_id>` (retained, Last-Will)
- **Scenes topic:** `picolight_lf26/scenes` (retained, client-to-client scene library)

All messages are JSON with a `from` field for echo suppression. The full contract is documented in [MQTT Protocol Specification](#mqtt-protocol-specification) — that section is the reference for writing an additional client.

### Colour Engine (`colour.py`)

The `ColourEngine` class manages per-group colour state:

- **Palette position** (`pos`, float 0.0–1.0): maps into a 30-stop hue palette blended with warm white. `0.0` = pure warm white (W channel only); higher values = saturated hues with W fading out.
- **Groups**: the strip is split into 1–N independent colour zones. Each group has a `pos` (hue) and a `w` (warm-white level). Group sizes are stored separately.
- **Fade**: each group independently crossfades from its start colour to its target over `FADE_STEPS` ticks, eased with smoothstep so the transition begins and ends imperceptibly. A fade takes the time it says it does — `fade_steps: 60` really is ~1 s.
- **Breathing**: a per-group sine oscillator adds a slow ±4% brightness pulse with staggered phases.
- **Drift**: optional autonomous colour drift — subtle hue nudges and group-size shifts every `IDLE_DRIFT_INTERVAL_S` seconds. Disabled by default in the web UI (`driftEnabled = false`).
- **Power**: soft on/off with an ~800 ms fade.

### BOSS / Follower Sync

When the BOSS's physical touch sensor fires, it runs `ColourEngine.impulse()` (random colour change), then publishes its new state with `"sync": true`. The follower applies the same groups with a fast fade (`SYNC_FADE_STEPS`) so both strips match immediately.

Every 60 s the BOSS publishes its state with `"sync": true` as a drift-correction heartbeat. The follower applies it with a slow 300-step fade so re-alignment is invisible. The BOSS only ever syncs a colour it has actually been given since boot (restored from flash, set by a client, a tap, or the partner): a board fresh from a reboot knows nothing but warm white, and before this guard it pushed that white onto the follower within a minute, undoing whatever the follower showed.

**Catching up.** A board that (re)connects publishes `{"state_request": true}`. The partner, if the lamps are linked, answers with its full state and `"sync": true`, and the newcomer adopts it with the slow fade. Whatever changed while a board was offline or rebooting, the partner knows. Lamps set apart on purpose (independent mode, below) do not answer, so they keep their own colours.

**Independent mode.** That periodic sync would otherwise undo deliberate per-lamp settings — set one lamp with **Send to → LS** and it reverted within a minute. So a **targeted** command (one carrying `target`) unlinks the lamps and suppresses the BOSS sync for 30 minutes; a **broadcast** colour command — which sets both lamps to the same thing — re-links them immediately. Boards notice targeted traffic even when it is addressed to the other lamp, so the BOSS knows to stand down.

### Alarms

Each board holds the whole schedule on its own flash (`alarms.json`) and evaluates it against its own clock, so **alarms fire with no client connected**. On each main-loop tick the board compares the current UTC time against enabled alarms and runs:

- **Sunrise** (`type: "sunrise"`): switches on and ramps brightness from 0 to `alarm.brightness` over `duration_min` minutes, sweeping **colour** alongside it — ember red → deep orange → amber → warm white. Because brightness rises together with the colour, the early stops are seen dim and deep, like a real dawn.
- **Bedtime** (`type: "sunset"`): the same sweep in reverse — warm white deepens to ember as it dims — then switches off (restoring the previous brightness so the next power-on isn't dark).

The gradient is `SUNRISE_STOPS` in `colour.py`, deliberately **not** taken from `TINT_PALETTE`: there a single `pos` sets hue and saturation together, so anything warm is also nearly white and a dawn would be an invisible tint.

Stops are evenly spaced by index, so where they sit is what shapes the fade. They are weighted toward the red end: the dawn opens on near-pure red `(255,6,0,0)`, the white LED stays completely dark for the first three fifths, and the turn to warm white happens in the last stretch. Red is never given up — the R channel is full the whole way — so it lands on a warm white with red still under it rather than a cold one. `tests/test_sunrise.py` pins that shape, so the stops stay tunable but the character can't drift back to turning orange almost immediately.

The schedule reaches boards two ways: the **retained** `P/alarms` topic, which boards subscribe to so an offline or rebooting board picks it up on reconnect, and a `set_alarms` message on `P/events` for boards that are already online. Alarms are compared in **UTC**; clients convert local time. See the [protocol spec](#alarms-1) for the exact format.

Two requirements: the board needs a **synced clock** (NTP at boot, retried hourly — the heartbeat's `ntp` field reports this, and the web UI warns when it's false), and it needs to have **received the schedule** (the heartbeat's `alarms` count confirms it). Use the **▶ Test** button on any alarm to run its fade immediately and verify the whole path.

### WiFi Watchdog

If the board loses its MQTT connection (WiFi down or broker unreachable) for more than 10 minutes (`WIFI_OFFLINE_REBOOT_MS = 600000`) it reboots to reconnect and pick up any OTA update. Known networks are the `WIFI_NETWORKS` list in `config.py` plus any saved to `networks.json` on flash, tried in order.

### State Persistence

On/off state, brightness **and the colour** (the groups: `pos`, `w`, `size`) are written to `state.json` and restored on boot — so the lamps come back exactly as they were after the nightly OTA reboot, the 10-minute offline watchdog, a crash or a hang. The colour is applied instantly, before the power state, so a lamp comes up in its colour rather than fading there from warm white. Until this, only power and brightness survived: every reboot was a warm-white reset, and with reboots as common as they are (one a day by design, more on a flaky connection) the lamps "kept going back to white".

Writes are deliberately rationed. The web app includes `on` in every state publish, so persisting on each message meant a flash erase/write on every slider tick (~8 per second while dragging) — needless wear, and each write stalls the render loop. State is written only when it actually changes: immediately on a power toggle, before any planned reboot, on a 5-minute flush that bounds what an unexpected power cut can lose, and — for colour — once, 30 s after the last change (`COLOUR_SAVE_DELAY_MS`), so a tap or a drag along the palette costs a single write.

### Daily OTA reboot

Each board reboots once a day at **17:00 UTC** (`OTA_HOUR_UTC` in `main.py`) to pull the latest firmware. State (on/off + brightness) is saved before the reboot and restored after, so the lamps come back as they were.

### Resilience layers

The firmware is designed to recover from anything without human intervention:

| Layer | Failure it handles |
|-------|--------------------|
| Hardware watchdog (8 s) | Firmware *hangs* — stuck DNS, dead socket, lockup → automatic reboot |
| Crash guard | Any unhandled exception → reboot in 5 s, OTA pulls a fix on the way up |
| OTA compile check | A broken push to master, truncated download, or HTML error page is **refused** — boards keep running the last good firmware |
| All-or-nothing OTA | Every file is downloaded and verified before *any* is installed, so a mid-update network drop can't leave a new `main.py` running against an old `colour.py` |
| Flash-write rationing | State is written only when it changes, so slider drags can't wear out the flash |
| Atomic file writes | Power cut mid-write can't corrupt firmware or state/alarm/network files |
| MQTT input validation | The events topic is on a public broker — every field is validated and clamped before touching the engine or flash; malformed messages are logged and dropped |
| MQTT watchdog | Broker unreachable > 10 min → reboot to re-establish everything |
| NTP retry | Failed clock sync at boot retries hourly (otherwise alarms would silently stay dead until the next reboot) |
| State persistence | On/off + brightness restored after any reboot |
| Periodic GC | Heap stays defragmented over weeks of uptime; free memory is reported in every heartbeat |

The watchdog can be disabled per board (`WATCHDOG_ENABLED = False` in `config.py`) while debugging over WebREPL.

---

## Web UI

`index.html` at the repo root is served via **GitHub Pages**. It connects to the HiveMQ broker over WebSockets. No install required — open it in any browser. On a phone you can **Add to Home Screen** to get it as a standalone full-screen app (web manifest included), and the app automatically reconnects when you return to it after backgrounding.

A service worker (`sw.js`) caches the app shell and the mqtt.js library, so the UI keeps working if the CDN is unreachable. HTML is fetched network-first, so a pushed update always lands rather than stranding you on a cached build.

### Controls

| Control | What it does |
|---------|-------------|
| **Power** | Soft on/off; state reflected in board pills immediately |
| **Tap** | Random colour change (same as physically touching the lamp) |
| **Warm White** | Resets all groups to `pos=0.0, w=1.0` (pure warm white) |
| **Brightness** | Global brightness 0–100% |
| **Fade Speed** | Colour transition duration (~0.3 s–10 s) |
| **Send to** | Target Both boards / FF only / LS only |

### Strip preview

The top card shows the RGBW strip in real time. Click the small divider cells below the strip to split or merge colour groups.

### Groups

Each group gets a numbered handle on the palette bar — drag it along the hue gradient to pick a colour. The **Warm White** slider below each group blends in warm white independently.

### Alarms

Create sunrise/bedtime alarms per lamp with day-of-week selection, duration, and target brightness.

Times are entered in **your local time**; the small `HH:MM UTC` hint beside each one shows what the lamps actually compare against. Because the firmware only knows UTC, the app stores your intended local time and recomputes the UTC value when needed, so an alarm set for 07:00 stays at 07:00 after a daylight-saving change.

Each alarm has a **▶ Test** button that runs its fade on both lamps immediately (20 s) — the quickest way to confirm the whole path works. A warning banner appears if a lamp's clock hasn't synced, since alarms can't fire without it.

### Scenes

Save and load complete lamp states (colours, group layout, brightness, fade speed).

Scenes are stored in `localStorage` **and** mirrored to the retained `P/scenes` topic, so your phone, your laptop and any other client converge on one shared library. The merge is deliberately conservative — no device can destroy another's work:

- scenes merge by `id`, keeping whichever copy has the newer `updated` stamp
- a scene simply missing from another device's list means "not seen yet", never "delete it"
- deletions travel as **tombstones**, so a deleted scene isn't helpfully synced back by a device that still has it (kept to the last 50)

### Settings panel

| Field | Default | Description |
|-------|---------|-------------|
| WebSocket URL | `wss://broker.hivemq.com:8884/mqtt` | MQTT broker |
| Topic Prefix | `picolight_lf26` | Must match boards |
| LEDs | `10` | LEDs per strip |
| Groups | `3` | Initial group count |

Also: **Add WiFi Network** (sends credentials to both boards over MQTT), **↺ Reboot Both Boards** (triggers OTA update).

### Board presence pills

The two pills (FF / LS) at the top right show each board's status. Presence uses MQTT **Last-Will** on a retained per-board topic (`prefix/status/<board_id>`): each board publishes `{"online": true}` (retained) on connect, and the broker itself flips it to `{"online": false}` if the board dies silently (within ~90 s of its last keepalive). Because the status is retained, the pills are correct the instant the page loads. The web app additionally pings both boards on connect and every 2 minutes to refresh their full state.

- **Green (online)** — retained status online (or message seen within 150 s) and lights on
- **Amber (standby)** — same, but lights are off
- **No highlight (offline)** — broker reported the board offline, or nothing heard within 150 s

---

## MQTT Protocol Specification

This is the complete contract. Any client (this web app, another web app,
Home Assistant, a script) only needs these topics — there is no other API.

### Topics

With `MQTT_TOPIC_PREFIX = "picolight_lf26"` (call it `P`):

| Topic | Direction | Retained | Payload |
|-------|-----------|----------|---------|
| `P/events` | client ⇄ board | no | Command / state object (below) |
| `P/alarms` | client → board | **yes, QoS 1** | Bare JSON **array** of alarm objects |
| `P/status/<board_id>` | board → client | **yes** | `{"online": bool, "fw": str, "ntp": bool}` |
| `P/scenes` | client ⇄ client | **yes** | Shared scene library (below). Boards ignore it entirely |

Board IDs are `board_a` (BOSS, display name FF) and `board_b` (follower, LS).

A client should subscribe to `P/events`, `P/alarms`, and `P/status/+`.

### Message envelope (`P/events`)

Every message on `P/events` is a JSON **object**. Non-objects are dropped.

| Field | Type | Meaning |
|-------|------|---------|
| `from` | string | **Required.** Sender ID. A board ignores messages where `from` equals its own ID. IDs starting with `board_` mark board-to-board traffic |
| `target` | string | Optional. If present, only the board with this `board_id` acts on the message |
| `echo` | bool | Optional. Marks an informational state report. **Boards ignore echo-flagged messages** — see below |
| `sync` | bool | Optional. Board-to-board: apply colours with a slow 300-step fade |
| `state_request` | bool | Optional. Board-to-board: "I just (re)connected, how do you look?" A linked partner answers with its state and `sync: true` |

> **The `echo` rule matters.** When a board answers a client, it replies with
> its full state plus `"echo": true`. Without that flag the *other* board
> would treat the reply as a real colour change and restart its fade — which
> caused a visible flash on every interaction. If you write a client, never
> set `echo` on commands you send; treat incoming `echo` messages as
> read-only state.

### Commands (client → board, on `P/events`)

All fields are optional; send only what you want to change. Every value is
validated and clamped by the firmware — out-of-range or wrong-typed fields
are ignored, never fatal.

| Field | Type | Range | Effect |
|-------|------|-------|--------|
| `groups` | array | 1…`NUM_LEDS` entries | Per-group colour, below |
| `on` | bool | | Power, with an ~800 ms soft fade |
| `brightness` | number | 0.0–1.0 | Global brightness |
| `fade_steps` | number | 1–2000 | Crossfade length in ~16 ms ticks (60 ≈ 1 s) |
| `drift_enabled` | bool | | Autonomous idle colour drift |
| `drift_interval` | number | 5–86400 | Seconds between drift events |
| `reverse` | bool | | Flip LED order |
| `ping` | any | | Ask boards to reply with their state (`echo: true`) |
| `set_alarms` | array | ≤20 alarms | Install a schedule (see Alarms) |
| `test_alarm` | `"sunrise"` \| `"sunset"` | | Run that fade **immediately** |
| `test_seconds` | number | 2–600 | Duration for `test_alarm` (default 20) |
| `add_network` | object | | `{"ssid": str≤32, "password": str≤64}` — saved to the board's flash |
| `reboot` | truthy | | Save state and restart (triggers an OTA check) |

A `groups` entry:

```json
{"pos": 0.35, "w": 0.8, "size": 3}
```

| Field | Type | Range | Meaning |
|-------|------|-------|---------|
| `pos` | number | 0.0–1.0 | Position in the hue palette. `0.0` = pure warm white (W channel only); higher = saturated tint with W fading out |
| `w` | number | 0.0–1.0 | Warm-white level for this group |
| `size` | integer | ≥1 | How many consecutive LEDs this group covers |

`size` values should sum to `NUM_LEDS`. If they sum to less, the firmware
extends the last group to cover the remainder (so no LED is left showing a
stale colour); if they sum to more, the extra is trimmed. The firmware
resizes its internal group arrays to match the array length, so a per-LED
gradient is simply `NUM_LEDS` groups of `size: 1`.

Example — set three groups and full brightness:

```json
{
  "from": "my_app",
  "groups": [
    {"pos": 0.0,  "w": 1.0, "size": 4},
    {"pos": 0.35, "w": 0.8, "size": 3},
    {"pos": 0.7,  "w": 0.6, "size": 3}
  ],
  "on": true,
  "brightness": 0.6,
  "fade_steps": 60
}
```

### Reports (board → client, on `P/events`)

**State echo** — sent whenever a board acts on a client message (including `ping`):

```json
{
  "from": "board_a", "echo": true,
  "groups": [{"pos": 0.0, "w": 1.0, "size": 10}],
  "on": true, "brightness": 0.6, "fade_steps": 60,
  "drift_enabled": false, "drift_interval": 45
}
```

**Heartbeat** — every 30 s:

```json
{"from": "board_a", "heartbeat": true, "fw": "2026-07-26.5",
 "mem": 102400, "ntp": true, "alarms": 2}
```

| Field | Meaning |
|-------|---------|
| `fw` | Firmware version |
| `mem` | Free heap bytes — watch for leaks |
| `ntp` | **Clock synced?** If `false`, alarms cannot fire — surface this to the user |
| `alarms` | How many alarms the board currently holds — confirms delivery |

**Impulse / boss sync** — a physical touch, or the BOSS's 60 s sync. Same
shape as a state echo but **without** `echo`, so the other board applies it.
Boss syncs carry `"sync": true`.

### Presence (`P/status/<board_id>`, retained)

```json
{"online": true, "fw": "2026-07-26.5", "ntp": true}
```

Registered as the board's MQTT **Last-Will** with payload `{"online": false}`,
so the *broker* publishes the offline state if a board dies silently
(~90 s, from the 60 s keepalive). Because it is retained, a client knows both
boards' state the instant it subscribes — no waiting for a heartbeat.

Recommended client logic: treat `online: false` as authoritative offline;
otherwise consider a board present if `online: true` or a message arrived
within ~150 s. Show offline if your own broker connection is down.

### Alarms

Two transports, both needed:

1. **`P/alarms`, retained, QoS 1** — the durable schedule. Boards subscribe
   to it, so one that was offline or rebooting when the schedule changed
   receives it on reconnect. Payload is a bare JSON **array**.
2. **`{"set_alarms": [...]}` on `P/events`** — the instant path for boards
   that are already online.

Publish **both** on every change, and **never** set `target` on an alarm
update: which lamp acts is decided by the alarm's own `boards` field.

```json
[
  {
    "enabled": true,
    "type": "sunrise",
    "hour": 5,
    "minute": 0,
    "duration_min": 30,
    "brightness": 1.0,
    "days": [0, 1, 2, 3, 4],
    "boards": ["board_a", "board_b"]
  }
]
```

| Field | Type | Range | Meaning |
|-------|------|-------|---------|
| `enabled` | bool | | Skipped when false |
| `type` | string | `sunrise` \| `sunset` | Fade up, or fade down and switch off |
| `hour` | int | 0–23 | **UTC** — see below |
| `minute` | int | 0–59 | **UTC** |
| `duration_min` | int | 1–180 | Ramp length in minutes |
| `brightness` | number | 0.0–1.0 | Sunrise target (ignored for sunset) |
| `days` | int array | 0=Mon … 6=Sun | Omit or send a non-array for *every day* |
| `boards` | string array | board IDs | Which lamps act. Empty/omitted = all |

Each board evaluates the schedule against **its own** UTC clock and runs its
own ramp, so alarms work even with no client connected. At most one alarm
fires per minute per board; a fired alarm is remembered until the next day
(keyed on hour+minute+type, so a sunrise and a sunset at the same time both
fire).

> **Timezone.** The Pico has no timezone database, so the firmware only
> understands UTC. Clients are responsible for converting. This web app edits
> in local time and stores the user's intent in two extra fields, `lh`/`lm`
> (local hour/minute), recomputing `hour`/`minute` on load so alarms keep
> their wall-clock time across a DST change. The firmware ignores `lh`/`lm`.
> **If you write another client, either use the same convention or send UTC
> directly** — and be aware this app will rewrite `hour`/`minute` from
> `lh`/`lm` if both are present.

To verify a schedule without waiting for the clock, send
`{"from": "my_app", "test_alarm": "sunrise", "test_seconds": 20}` — the
board runs the real alarm code path immediately.

### Scenes (retained on `P/scenes`)

Boards ignore this topic entirely; it exists so multiple clients share one
scene library. Publish **retained** so a client that connects later gets it.

```json
{
  "v": 1,
  "from": "web_app",
  "scenes": [
    {
      "id": "lz4f8x-a91b2c",
      "updated": 1774598400000,
      "name": "Evening",
      "groups": [{"pos": 0.0, "w": 1.0, "size": 10}],
      "boundaries": [], "groupPositions": [0.0], "groupWLevels": [1.0],
      "brightness": 0.4, "fadeSteps": 120, "on": true
    }
  ],
  "deleted": ["old-scene-id"]
}
```

| Field | Meaning |
|-------|---------|
| `id` | Stable unique string. **Required** for a scene to take part in sync |
| `updated` | Epoch ms of the last edit — the conflict tiebreak |
| `deleted` | Tombstoned ids, capped at the most recent 50 |

Merge rules (implement these and two clients can't fight):

- union by `id`; on a clash keep the higher `updated`
- a scene absent from the other side means "not seen yet", **not** "delete"
- honour incoming tombstones, and keep publishing your own so a delete
  doesn't get synced back by a device that still holds the scene
- if the merge changed nothing, don't republish — otherwise two clients
  ping-pong retained messages forever

Everything except `id`, `updated` and `name` is opaque payload; this app
stores the fields shown, but another client may store whatever it needs.

### Guarantees for client authors

- Malformed messages are logged and dropped; they never crash a board or
  drop its MQTT connection.
- Unknown fields are ignored, so the protocol can be extended safely.
- Boards persist power state, brightness, and alarms to flash and restore
  them after any reboot.
- The events topic is on a **public broker**. Anyone who knows your prefix
  can control your lamps — pick an unguessable `MQTT_TOPIC_PREFIX`, or run
  your own broker with authentication.

---

## First-Time Setup

1. **Flash MicroPython** — download the Pico W UF2 from micropython.org, hold BOOTSEL, drag it on.

2. **Install umqtt** (in Thonny shell or `mpremote`):
   ```python
   import mip
   mip.install("umqtt.simple")
   ```

3. **Upload files** (only once):
   ```bash
   mpremote connect /dev/ttyACM0 cp boot.py config.py :
   ```
   Set your WiFi credentials in `WIFI_NETWORKS` in the board's `config.py`
   (or create `networks.json` on the board — a list of `[ssid, password]` pairs):
   ```json
   [["YourNetwork", "YourPassword"]]
   ```

4. **Edit `config.py`** — set `BOARD_ID`, `BOSS`, `NUM_LEDS`, `LED_PIN`, `MQTT_TOPIC_PREFIX`.

5. **Reboot** — `boot.py` downloads `main.py`, `colour.py`, `sk6812.py`, `touch.py` from master and starts.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Board B LEDs don't light | Connected to DOUT | Run wire to DIN end |
| Wrong colours | Byte-order mismatch | SK6812 uses GRBW — check `sk6812.py` |
| Groups reversed on board B | Far-end connector | `REVERSE_LEDS = True` in `config.py` |
| Touch never fires | Threshold too high | Lower `TOUCH_THRESHOLD` in `config.py` |
| Touch fires constantly | Missing pull-down | 1 MΩ between GPIO and GND |
| Boards not syncing | Different topic prefix | `MQTT_TOPIC_PREFIX` must match on both |
| Board reboots after ~10 min offline | WiFi watchdog | Expected; board reconnects and checks OTA |
| **Alarm fires at the wrong time** | Alarm times are UTC; older clients sent local time as UTC | Times are now edited in local time and converted. Check the small `HH:MM UTC` hint next to each alarm |
| **Alarm never fires** | Board has no clock (NTP failed) | The web UI shows a warning banner; heartbeat `ntp` is false. Retries hourly, or reboot the board |
| **Alarm never fires**, clock is fine | Board never received the schedule | Heartbeat `alarms` count shows 0. Fixed by the retained `P/alarms` topic; press **▶ Test** to confirm the path |
| Alarm only fires on one lamp | Alarm's `boards` list, or a client sending `target` with the update | Enable both lamps on the alarm; never set `target` on alarm updates |
| OTA not updating | `boot.py` not on board | Flash `boot.py` manually once via USB |

---

## Project Structure

```
linked_friend_lights/
├── index.html              # Web UI — single source, served by GitHub Pages
├── sw.js                   # Service worker — caches shell + mqtt.js
├── manifest.json           # PWA manifest (Add to Home Screen)
├── main.py                 # Firmware — main loop (OTA-fetched)
├── colour.py               # Firmware — colour engine (OTA-fetched)
├── sk6812.py               # Firmware — PIO LED driver (OTA-fetched)
├── touch.py                # Firmware — capacitive touch (OTA-fetched)
├── boot.py                 # OTA bootstrap (deployed manually, never overwritten)
├── config.py               # Per-board config TEMPLATE — real credentials live
│                           #   only on the boards, never in the repo
├── hardware_test.py        # Standalone wiring test (no WiFi/MQTT needed)
├── favicon.svg
└── README.md
```

> **Security note:** this repo is public (it serves the web UI via GitHub Pages).
> Never commit real WiFi or WebREPL passwords — edit them only in the `config.py`
> that lives on each board, which OTA never touches.

---

Personal project — no license.

---

## Tests

```bash
python3 tests/test_firmware.py     # boots main(), MQTT commands, flash writes
python3 tests/test_ota.py          # OTA all-or-nothing install
python3 tests/test_rollback.py     # crash-loop rollback
python3 tests/test_sunrise.py      # dawn gradient shape
node    tests/test_scene_sync.js   # scene merge / tombstone rules
```

Runs `main()` against stubbed MicroPython modules (`tests/stubs/`) and checks
that the board boots, arms and feeds the watchdog, subscribes to the events
**and** retained alarms topics, registers its Last-Will, publishes presence and
heartbeats, reboots on command, installs a retained alarm schedule, applies
colour commands, survives malformed input, persists the colour (debounced) and
restores it after a reboot, never syncs an unknown colour, answers a partner's
`state_request` only while linked, and adopts the partner's answer with the
slow fade.

`test_ota.py` drives `boot.py`'s updater against a stubbed HTTP layer and
asserts that a network drop, a syntax error, or an HTTP error part-way
through leaves the existing firmware completely untouched.

Run both before pushing to `master`. `python -m py_compile` only catches syntax
errors — it cannot catch a runtime fault such as a stray function-level
`import`, which once shadowed a module-level import for the whole of `main()`
and put every board into a boot loop.
