# ============================================================
#  main.py  —  Entry point, runs on BOTH boards identically
#  Only config.py differs between boards.
# ============================================================

import gc
import math
import sys
import os
import utime
import ujson
import machine
import network
from umqtt.simple import MQTTClient

from config import (
    BOARD_ID, BOSS,
    WIFI_NETWORKS,
    MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASSWORD,
    MQTT_TOPIC_PREFIX,
    LED_PIN, NUM_LEDS, LED_BRIGHTNESS,
    TOUCH_PINS,
    RECONNECT_DELAY_MS,
    WEBREPL_PASSWORD,
)

FIRMWARE_VERSION       = "2026-08-25.1"
FRAME_MS               = 16
SYNC_INTERVAL_MS       = 60_000
SYNC_FADE_STEPS        = 300
WIFI_OFFLINE_REBOOT_MS = 10 * 60 * 1000   # reboot after 10 min offline

# Boards keep their own (never-OTA'd) config.py, so new config flags must
# be imported with a fallback or older boards would ImportError on boot.
try:
    from config import WATCHDOG_ENABLED
except ImportError:
    WATCHDOG_ENABLED = True

# ── Hardware watchdog ────────────────────────────────────────
# The crash guard catches exceptions, but not HANGS (stuck DNS lookup,
# dead socket, PIO lockup). The RP2040 watchdog reboots the board if the
# main loop stops feeding it for 8 s — a hang becomes a 1-off restart
# instead of a dead lamp.
wdt = None

def feed_wdt():
    if wdt:
        wdt.feed()
from sk6812 import SK6812
from touch   import TouchManager
from colour  import ColourEngine

# ── MQTT topics ─────────────────────────────────────────────
# All boards publish to and subscribe from the same shared topic.
# They ignore messages that carry their own BOARD_ID (echo prevention).
TOPIC_EVENTS = (MQTT_TOPIC_PREFIX + "/events").encode()
# Retained per-board presence topic. A Last-Will makes the BROKER flip it
# to offline if the board dies silently; retained delivery means a web
# client knows each board's state the instant it subscribes.
TOPIC_STATUS = (MQTT_TOPIC_PREFIX + "/status/" + BOARD_ID).encode()
# Retained alarm schedule, shared by all clients and boards. Subscribing to
# it is what makes alarms durable: a board that was offline (or rebooting)
# when the schedule changed still receives it the moment it reconnects.
TOPIC_ALARMS = (MQTT_TOPIC_PREFIX + "/alarms").encode()

ntp_ok = False        # module-level so status/heartbeat can report clock health
_test_alarm = None    # set by MQTT test_alarm, consumed by the main loop

# The BOSS re-publishes its state every 60 s so the lamps converge. That
# also silently undid deliberate per-lamp settings ("Send to → LS" reverted
# within a minute). A targeted command therefore unlinks the lamps, and a
# broadcast command — which sets both to the same thing — re-links them.
INDEPENDENT_MS   = 30 * 60 * 1000
_independent_until = None   # ticks_ms deadline, or None when linked

# ── Module-level objects ─────────────────────────────────────
strip  = SK6812(pin=LED_PIN, num_leds=NUM_LEDS, brightness=LED_BRIGHTNESS)
touch  = TouchManager(TOUCH_PINS)
engine = ColourEngine()
client = None   # MQTTClient, initialised after WiFi

# ── WiFi ─────────────────────────────────────────────────────

NETWORKS_FILE = "networks.json"

def _write_json(fname, obj):
    """Atomic JSON write: temp file + rename, so a power cut mid-write
    can never leave a truncated/corrupt file on flash."""
    tmp = fname + ".tmp"
    with open(tmp, "w") as f:
        ujson.dump(obj, f)
    os.rename(tmp, fname)

def load_extra_networks():
    """Load user-added networks from networks.json (junk entries dropped)."""
    try:
        with open(NETWORKS_FILE) as f:
            nets = ujson.load(f)
        return [n for n in nets
                if isinstance(n, (list, tuple)) and len(n) == 2
                and isinstance(n[0], str) and isinstance(n[1], str)]
    except Exception:
        return []

def save_network(ssid, password):
    """Append a new network to networks.json, avoiding duplicates."""
    nets = load_extra_networks()
    if any(n[0] == ssid for n in nets):
        print(f"[wifi] {ssid} already saved")
        return
    nets.append([ssid, password])
    _write_json(NETWORKS_FILE, nets)
    print(f"[wifi] saved new network: {ssid}")

def all_networks():
    return WIFI_NETWORKS + load_extra_networks()


# ── Crash-loop rollback (paired with boot.py) ────────────────
# boot.py counts boots that never reach stability and restores `.ok`
# backups after too many. Our job is to declare this firmware good once it
# has actually run for a while. Constants mirror boot.py, which is not
# OTA-updated and so cannot be imported from.
STABLE_MS     = 60_000
FAIL_FILE     = "bootfail.txt"
ROLLBACK_FLAG = "rolledback.txt"
OK_SUFFIX     = ".ok"
SYNCED_FILES  = ("main.py", "colour.py", "sk6812.py", "touch.py")

def _rolled_back():
    try:
        open(ROLLBACK_FLAG).close()
        return True
    except OSError:
        return False

def mark_stable():
    """This firmware has run long enough to be trusted: clear the crash
    counter and promote the running files to known-good backups."""
    try:
        with open(FAIL_FILE, "w") as f:
            f.write("0")
    except Exception as e:
        print(f"[boot] could not clear fail counter: {e}")
    try:
        os.remove(ROLLBACK_FLAG)     # recovered; stop reporting it
    except OSError:
        pass
    for fname in SYNCED_FILES:
        try:
            with open(fname) as f:
                current = f.read()
            try:
                with open(fname + OK_SUFFIX) as f:
                    if f.read() == current:
                        continue         # backup already matches — no write
            except OSError:
                pass                     # no backup yet
            tmp = fname + OK_SUFFIX + ".tmp"
            with open(tmp, "w") as f:
                f.write(current)
            os.rename(tmp, fname + OK_SUFFIX)
            print(f"[boot] known-good backup updated: {fname}")
        except Exception as e:
            print(f"[boot] backup of {fname} failed: {e}")
    print("[boot] firmware marked stable")

def connect_wifi(tick=None):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan

    print("[wifi] scanning...")
    visible = set()
    try:
        for ap in wlan.scan():
            visible.add(ap[0].decode("utf-8", "ignore"))
    except Exception:
        pass
    print(f"[wifi] visible networks: {visible}")

    for ssid, password in all_networks():
        if visible and ssid not in visible:
            print(f"[wifi] {ssid} not in range, skipping")
            continue
        print(f"[wifi] trying {ssid}...")
        wlan.connect(ssid, password)
        deadline = utime.ticks_add(utime.ticks_ms(), 12_000)
        while not wlan.isconnected():
            if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
                print(f"[wifi] {ssid} timed out")
                wlan.disconnect()
                utime.sleep_ms(500)
                break
            if tick:
                tick()
            utime.sleep_ms(50)
        if wlan.isconnected():
            print(f"[wifi] connected to {ssid} — IP {wlan.ifconfig()[0]}")
            return wlan

    raise OSError("No known WiFi network found")


# ── MQTT ─────────────────────────────────────────────────────

# ── Input validation ─────────────────────────────────────────
# The events topic lives on a PUBLIC broker — anything can be published to
# it. Every field is validated/clamped before it touches the engine or,
# worse, gets persisted to flash. (Before this, one malformed set_alarms
# message would be saved to alarms.json and crash the main loop on every
# boot — a permanent remote boot-loop.)

def _num(v, lo, hi, default=None):
    """Coerce to float and clamp to [lo, hi]; default if not a number."""
    try:
        f = float(v)
    except Exception:
        return default
    return max(lo, min(hi, f))

def _sanitise_groups(groups):
    """Validated copy of a groups list, or None if unusable."""
    if not isinstance(groups, list) or not groups:
        return None
    if len(groups) > NUM_LEDS:          # cap — huge lists would exhaust RAM
        groups = groups[:NUM_LEDS]
    clean = []
    for g in groups:
        if not isinstance(g, dict):
            return None
        pos = _num(g.get("pos"), 0.0, 1.0)
        w   = _num(g.get("w"),   0.0, 1.0)
        size = g.get("size")
        if pos is None or w is None or not isinstance(size, (int, float)) or size < 1:
            return None
        clean.append({"pos": pos, "w": w, "size": int(size)})
    return clean

def _sanitise_alarms(data):
    """Validated copy of an alarm list, or None if not a list."""
    if not isinstance(data, list):
        return None
    clean = []
    for a in data[:20]:                 # cap alarm count
        if not isinstance(a, dict):
            continue
        try:
            clean.append({
                "enabled":      bool(a.get("enabled", True)),
                "type":         "sunset" if a.get("type") == "sunset" else "sunrise",
                "hour":         max(0, min(23, int(a.get("hour", 0)))),
                "minute":       max(0, min(59, int(a.get("minute", 0)))),
                "duration_min": max(1, min(180, int(a.get("duration_min", 30)))),
                "brightness":   _num(a.get("brightness", 1.0), 0.0, 1.0, 1.0),
                "days":   [d for d in a["days"] if isinstance(d, int) and 0 <= d <= 6]
                          if isinstance(a.get("days"), list) else list(range(7)),
                "boards": [b for b in a.get("boards", []) if isinstance(b, str)],
            })
        except Exception:
            continue                    # skip one bad alarm, keep the rest
    return clean


def on_message(topic, msg):
    """Called by the MQTT library when a message arrives.
    Must NEVER raise — an exception here tears down the MQTT connection,
    so a single bad message could trigger a reconnect storm."""
    try:
        data = ujson.loads(msg)
    except Exception:
        return
    try:
        # The alarms topic carries a bare JSON array (retained schedule)
        if topic == TOPIC_ALARMS:
            _apply_alarms(data)
            return
        if not isinstance(data, dict):
            return
        _handle_message(data)
    except Exception as e:
        sys.print_exception(e)
        print("[mqtt] bad message ignored")


def _apply_alarms(data):
    """Install a new alarm schedule from either transport."""
    clean = _sanitise_alarms(data)
    if clean is None:
        return
    save_alarms(clean)
    print(f"[alarm] schedule updated — {len(clean)} alarm(s)")
    for a in clean:
        if a["enabled"] and (not a["boards"] or BOARD_ID in a["boards"]):
            print("[alarm]   {:02d}:{:02d} UTC {} days={}".format(
                a["hour"], a["minute"], a["type"], a["days"]))


def _handle_message(data):
    # Ignore our own echoed messages
    if data.get("from") == BOARD_ID:
        return

    # Note independence BEFORE the target filter, so the BOSS also sees
    # commands aimed at the follower and stops overwriting them.
    target = data.get("target")
    sender = data.get("from", "")
    if sender and not sender.startswith("board_"):
        global _independent_until
        if target is not None:
            _independent_until = utime.ticks_add(utime.ticks_ms(), INDEPENDENT_MS)
        elif data.get("groups") is not None:
            _independent_until = None   # both lamps just got the same state

    # Ignore messages targeted at a different board
    if target is not None and target != BOARD_ID:
        return

    # State echoes are for web clients only. Without this, every web
    # command made each board re-apply the OTHER board's echo and restart
    # its fade — the visible stutter/flash on every interaction.
    if data.get("echo"):
        return

    print(f"[mqtt] recv: {data}")
    groups         = _sanitise_groups(data.get("groups"))
    on             = data.get("on")
    brightness     = _num(data.get("brightness"), 0.0, 1.0)
    reverse        = data.get("reverse")
    fade_steps     = _num(data.get("fade_steps"), 1, 2000)
    drift_enabled  = data.get("drift_enabled")
    drift_interval = _num(data.get("drift_interval"), 5, 86400)

    if on is not None:
        # Only touch flash when the power state actually flips. The web app
        # includes `on` in every state publish, so persisting unconditionally
        # meant a flash erase/write on every slider tick (~8/s while dragging)
        # — needless wear, and each write stalls the render loop.
        was_on = engine._powered_on
        engine.set_power(bool(on))
        if engine._powered_on != was_on:
            save_state()
    if brightness is not None:
        engine.set_brightness(float(brightness))
    if reverse is not None:
        engine.set_reverse(bool(reverse))
    if fade_steps is not None:
        engine.set_fade_steps(int(fade_steps))
    if drift_enabled is not None:
        engine.set_drift_enabled(bool(drift_enabled))
    if drift_interval is not None:
        engine.set_drift_interval(int(drift_interval))
    if groups is not None and on is not False:
        fade_override = SYNC_FADE_STEPS if data.get("sync") else None
        engine.force_colour(groups, fade_steps_override=fade_override)

    add_net = data.get("add_network")
    if (isinstance(add_net, dict)
            and isinstance(add_net.get("ssid"), str)
            and 0 < len(add_net["ssid"]) <= 32
            and isinstance(add_net.get("password"), str)
            and len(add_net["password"]) <= 64):
        save_network(add_net["ssid"], add_net["password"])

    if "set_alarms" in data:
        _apply_alarms(data["set_alarms"])

    # Run an alarm ramp immediately, so the schedule can be verified
    # without waiting for the wall clock. Value: "sunrise" | "sunset".
    ta = data.get("test_alarm")
    if ta in ("sunrise", "sunset") or ta is True:
        global _test_alarm
        secs = _num(data.get("test_seconds"), 2, 600, 20)
        _test_alarm = {"type": "sunset" if ta == "sunset" else "sunrise",
                       "dur_ms": int(secs * 1000)}
        print(f"[alarm] test requested — {_test_alarm['type']} over {secs:.0f}s")

    if data.get("reboot"):
        print("[mqtt] reboot requested — saving state and rebooting")
        # Ack before resetting, so the web UI can tell "rebooting now" apart
        # from "message ignored by firmware too old to know reboot". Without
        # this the button gives no sign of life until the board drops offline,
        # which looks identical to the button being broken.
        try:
            publish_event({"rebooting": True, "fw": FIRMWARE_VERSION})
        except Exception:
            pass
        save_state()
        machine.reset()

    # Echo current state back to web clients so the UI stays in sync.
    # Only echo messages that didn't come from another board (avoids loops).
    if sender and not sender.startswith("board_"):
        payload = engine.get_event_payload()
        payload["echo"] = True
        publish_event(payload)


def connect_mqtt() -> MQTTClient:
    import ubinascii, machine
    uid = ubinascii.hexlify(machine.unique_id()).decode()
    client_id = f"{BOARD_ID}_{uid}".encode()
    c = MQTTClient(
        client_id,
        MQTT_BROKER,
        port=MQTT_PORT,
        user=MQTT_USER.encode() if MQTT_USER else None,
        password=MQTT_PASSWORD.encode() if MQTT_PASSWORD else None,
        keepalive=60,
    )
    c.set_callback(on_message)
    c.set_last_will(TOPIC_STATUS, b'{"online": false}', retain=True)
    c.connect()
    c.subscribe(TOPIC_EVENTS)
    c.subscribe(TOPIC_ALARMS)   # retained → schedule redelivered on every connect
    c.publish(TOPIC_STATUS,
              ujson.dumps({"online": True, "fw": FIRMWARE_VERSION,
                           "ntp": ntp_ok}),
              retain=True)
    print(f"[mqtt] connected — client_id={client_id}, topic={TOPIC_EVENTS}")
    return c


def publish_event(payload: dict):
    """Publish a colour/power event to the shared topic."""
    global client
    if client is None:
        return
    payload["from"] = BOARD_ID
    try:
        client.publish(TOPIC_EVENTS, ujson.dumps(payload))
    except Exception as e:
        print(f"[mqtt] publish error: {e}")
        client = None   # trigger reconnect


OTA_HOUR_UTC = 17   # 5 pm UTC — daily reboot triggers boot.py OTA

STATE_FILE  = "state.json"
ALARM_FILE  = "alarms.json"

# ── Alarms ───────────────────────────────────────────────────

_alarms = []

def load_alarms():
    global _alarms
    try:
        with open(ALARM_FILE) as f:
            # Re-sanitise on load — protects against legacy junk on flash
            _alarms = _sanitise_alarms(ujson.load(f)) or []
    except Exception:
        _alarms = []

def save_alarms(data):
    global _alarms
    _alarms = data
    try:
        _write_json(ALARM_FILE, data)
    except Exception as e:
        print(f"[alarm] save failed: {e}")

_saved_state = None

def save_state():
    """Persist power + brightness. Skips the write when nothing changed, so
    callers can invoke it freely without wearing out the flash."""
    global _saved_state
    snap = (engine._powered_on, round(engine._brightness, 3))
    if snap == _saved_state:
        return
    try:
        _write_json(STATE_FILE, {"on": snap[0], "brightness": snap[1]})
        _saved_state = snap
    except Exception as e:
        print(f"[state] save failed: {e}")

def restore_state():
    global _saved_state
    try:
        with open(STATE_FILE) as f:
            d = ujson.load(f)
        br = d.get("brightness")
        if br is not None:
            engine.set_brightness(float(br))
        if not d.get("on", True):
            engine.set_power(False)
            engine._power_level = 0.0  # instant off, no fade-in
            print("[state] restored: off")
        else:
            print(f"[state] restored: on, brightness {br}")
        # Seed the snapshot so an unchanged state never rewrites flash
        _saved_state = (engine._powered_on, round(engine._brightness, 3))
    except Exception:
        pass  # no saved state — use default (on)

def sync_ntp():
    try:
        import ntptime
        ntptime.settime()
        print(f"[ntp] time synced: {utime.localtime()}")
        return True
    except Exception as e:
        print(f"[ntp] sync failed: {e}")
        return False


# ── Setup pulse ──────────────────────────────────────────────

class SetupPulser:
    """Soft warm-white pulse played on the strip during boot setup.
    Call tick() as often as possible inside blocking setup loops.
    Call stop() once setup is complete — it fades out cleanly.
    """
    _SPEED  = 0.004   # radians per ms — ~1.5 s per full breath
    _W_MID  = 60      # midpoint brightness (0-255 W channel)
    _W_AMP  = 50      # amplitude around midpoint

    def __init__(self):
        self._phase    = 0.0
        self._last_ms  = utime.ticks_ms()
        self.active    = True

    def tick(self):
        feed_wdt()   # setup's blocking waits call tick — keep the WDT fed
        if not self.active:
            return
        now = utime.ticks_ms()
        dt  = utime.ticks_diff(now, self._last_ms)
        self._last_ms = now
        self._phase  += self._SPEED * dt
        w = int(self._W_MID + self._W_AMP * math.sin(self._phase))
        strip.set_all(0, 0, 0, w)
        strip.show()

    def stop(self):
        """Fade out over ~400 ms then hand off to the engine."""
        self.active = False
        w = int(self._W_MID + self._W_AMP * math.sin(self._phase))
        for step in range(20):
            w = int(w * (1 - step / 20))
            strip.set_all(0, 0, 0, w)
            strip.show()
            utime.sleep_ms(20)
        strip.off()


# ── Main loop ────────────────────────────────────────────────

def main():
    global client, wdt, ntp_ok, _test_alarm

    print(f"[boot] firmware {FIRMWARE_VERSION} — board {BOARD_ID}")

    if WATCHDOG_ENABLED and wdt is None:
        wdt = machine.WDT(timeout=8000)
        print("[wdt] hardware watchdog armed (8 s)")

    # Pulse the strip immediately — visible feedback while setup runs
    pulser = SetupPulser()

    # Connect WiFi — pulse continues through the blocking connect loop
    wifi_ok = False
    try:
        connect_wifi(tick=pulser.tick)
        wifi_ok = True
    except OSError as e:
        print(f"[wifi] failed: {e} — running offline")

    pulser.tick()

    # Start WebREPL if WiFi is up — allows wireless file editing
    if wifi_ok:
        try:
            import webrepl
            webrepl.start(password=WEBREPL_PASSWORD)
            print(f"[webrepl] started — connect at ws://<board-ip>:8266")
        except Exception as e:
            print(f"[webrepl] failed to start: {e}")

    pulser.tick()

    # Sync NTP so we know the wall-clock time for daily OTA
    ntp_ok = False
    if wifi_ok:
        ntp_ok = sync_ntp()

    pulser.tick()

    # Connect MQTT — best-effort, continue without sync if it fails
    try:
        client = connect_mqtt()
    except Exception as e:
        print(f"[mqtt] connect failed: {e} — running standalone")

    pulser.tick()

    # Calibrate touch baseline (nobody touching at boot)
    print("[touch] calibrating baseline...")
    touch.calibrate_all()
    print("[touch] ready")

    # Restore power state saved before the last OTA reboot
    restore_state()

    # Load saved alarms
    load_alarms()

    # Setup complete — fade out the pulse and hand off to the engine
    pulser.stop()

    _ota_check_min  = -1
    _alarm_checked_min = -1
    _alarm_day      = -1
    _sunrise = {"active": False, "start_ms": 0, "dur_ms": 0, "target_br": 1.0}
    _alarm_fired = set()  # (hour, minute) pairs fired this calendar day
    last_frame      = utime.ticks_ms()
    _reconnect_at   = utime.ticks_add(utime.ticks_ms(), RECONNECT_DELAY_MS)
    _ping_at        = utime.ticks_add(utime.ticks_ms(), 20_000)
    _sync_at        = utime.ticks_add(utime.ticks_ms(), SYNC_INTERVAL_MS)
    _heartbeat_at   = utime.ticks_add(utime.ticks_ms(), 5_000)  # first beat soon after boot
    _status_at      = utime.ticks_add(utime.ticks_ms(), 300_000)
    _ntp_retry_at   = utime.ticks_add(utime.ticks_ms(), 60_000)
    _state_flush_at = utime.ticks_add(utime.ticks_ms(), 300_000)
    _stable_at      = utime.ticks_add(utime.ticks_ms(), STABLE_MS)
    _marked_stable  = False
    _was_rolled_back = _rolled_back()
    if _was_rolled_back:
        print("[boot] running RESTORED firmware — a previous version crash-looped")
    _backoff_ms     = RECONNECT_DELAY_MS
    _offline_since  = None   # when client first became None (WiFi watchdog)

    while True:
        now = utime.ticks_ms()
        feed_wdt()

        # ── Reconnect MQTT with exponential backoff ──
        if client is None and utime.ticks_diff(now, _reconnect_at) >= 0:
            try:
                # Ensure WiFi is up before attempting MQTT
                wlan = network.WLAN(network.STA_IF)
                if not wlan.isconnected():
                    print("[wifi] lost — reconnecting before MQTT retry")
                    # Keep rendering (and the watchdog fed) while the
                    # blocking reconnect runs — otherwise the lamps freeze
                    # mid-breath for up to ~12 s
                    connect_wifi(tick=lambda: (feed_wdt(), engine.tick(strip)))
                client = connect_mqtt()
                _backoff_ms    = RECONNECT_DELAY_MS   # reset on success
                _ping_at       = utime.ticks_add(now, 20_000)
            except Exception as e:
                print(f"[mqtt] reconnect failed: {e}")
                _backoff_ms = min(_backoff_ms * 2, 60_000)  # cap at 60 s
            _reconnect_at = utime.ticks_add(now, _backoff_ms)

        # ── Keepalive ping ──
        if client is not None and utime.ticks_diff(now, _ping_at) >= 0:
            try:
                client.ping()
            except Exception as e:
                print(f"[mqtt] ping error: {e}")
                client = None
            _ping_at = utime.ticks_add(now, 20_000)

        # ── Poll MQTT for incoming messages ──
        if client is not None:
            try:
                client.check_msg()    # non-blocking poll
            except Exception as e:
                print(f"[mqtt] check_msg error: {e}")
                client = None

        # ── WiFi watchdog — reboot if offline > 10 min ──
        if client is None:
            if _offline_since is None:
                _offline_since = now
            elif utime.ticks_diff(now, _offline_since) >= WIFI_OFFLINE_REBOOT_MS:
                print("[wifi] offline > 10 min — rebooting to recover connection")
                save_state()
                machine.reset()
        else:
            _offline_since = None

        # ── Boss sync — publish current state every 60 s for follower to drift to ──
        if BOSS and client is not None and utime.ticks_diff(now, _sync_at) >= 0:
            if _independent_until is not None and utime.ticks_diff(_independent_until, now) > 0:
                pass   # lamps deliberately set apart — don't overwrite
            else:
                payload = engine.get_event_payload()
                payload["sync"] = True
                publish_event(payload)
                print("[sync] boss published state")
            _sync_at = utime.ticks_add(now, SYNC_INTERVAL_MS)

        # ── Heartbeat — announce presence every 30 s ──
        if client is not None and utime.ticks_diff(now, _heartbeat_at) >= 0:
            # Periodic GC keeps the heap defragmented over weeks of uptime;
            # mem in the heartbeat gives free observability of leaks
            gc.collect()
            # ntp/alarms let a client warn "alarms can't fire" instead of
            # the schedule failing silently when the clock never synced
            hb = {"heartbeat": True, "fw": FIRMWARE_VERSION,
                  "mem": gc.mem_free(),
                  "ntp": ntp_ok, "alarms": len(_alarms)}
            if _was_rolled_back:
                hb["rolled_back"] = True   # surfaces a rejected firmware push
            publish_event(hb)
            _heartbeat_at = utime.ticks_add(now, 30_000)

        # ── Refresh retained presence every 5 min ──
        # Insurance against the public broker dropping retained state
        if client is not None and utime.ticks_diff(now, _status_at) >= 0:
            try:
                client.publish(TOPIC_STATUS,
                               ujson.dumps({"online": True, "fw": FIRMWARE_VERSION,
                                            "ntp": ntp_ok}),
                               retain=True)
            except Exception as e:
                print(f"[mqtt] status publish error: {e}")
                client = None
            _status_at = utime.ticks_add(now, 300_000)

        # ── Declare the firmware good once it has run a full minute ──
        if not _marked_stable and utime.ticks_diff(now, _stable_at) >= 0:
            mark_stable()
            _marked_stable = True

        # ── Flush state if it drifted (no-op when unchanged) ──
        # Brightness is no longer persisted on every message, so this
        # bounds what an unexpected power cut can lose to ~5 min.
        if utime.ticks_diff(now, _state_flush_at) >= 0:
            save_state()
            _state_flush_at = utime.ticks_add(now, 300_000)

        # ── NTP retry — a board that booted without NTP has no clock,
        #    which would silently disable alarms + daily OTA forever ──
        if not ntp_ok and client is not None and utime.ticks_diff(now, _ntp_retry_at) >= 0:
            ntp_ok = sync_ntp()
            _ntp_retry_at = utime.ticks_add(now, 3_600_000)

        # ── Daily OTA reboot at OTA_HOUR_UTC — save state first ──
        if ntp_ok:
            t = utime.localtime()
            cur_min = t[4]  # minute within hour
            if t[3] == OTA_HOUR_UTC and cur_min != _ota_check_min:
                _ota_check_min = cur_min
                if cur_min == 0:
                    print("[ota] daily maintenance window — saving state and rebooting for OTA")
                    save_state()
                    machine.reset()

        # ── Sunrise alarm check ──
        if ntp_ok and _alarms:
            t = utime.localtime()
            cur_min = t[4]
            if cur_min != _alarm_checked_min:
                _alarm_checked_min = cur_min
                # New day → clear the fired set. Day-of-year compare is
                # robust even if the exact midnight minute is missed
                # (the old ==00:00 check could block alarms forever).
                if t[7] != _alarm_day:
                    _alarm_day = t[7]
                    _alarm_fired.clear()
                for alarm in _alarms:
                    # Type is part of the key so a sunrise and a sunset
                    # scheduled at the same HH:MM don't mask each other
                    key = (alarm.get("hour", 0), alarm.get("minute", 0),
                           alarm.get("type", "sunrise"))
                    boards = alarm.get("boards", [])
                    if (alarm.get("enabled", True)
                            and t[3] == key[0] and cur_min == key[1]
                            and t[6] in alarm.get("days", list(range(7)))
                            and key not in _alarm_fired
                            and (not boards or BOARD_ID in boards)):
                        _alarm_fired.add(key)
                        dur_ms  = int(alarm.get("duration_min", 30)) * 60_000
                        alarm_type = alarm.get("type", "sunrise")
                        if alarm_type == "sunset":
                            _sunrise["active"]    = True
                            _sunrise["start_ms"]  = now
                            _sunrise["dur_ms"]    = dur_ms
                            _sunrise["target_br"] = 0.0   # ramp down to off
                            _sunrise["sunset"]    = True
                            _sunrise["start_br"]  = engine._brightness
                            print(f"[alarm] sunset! {key[0]:02d}:{key[1]:02d}")
                        else:
                            _sunrise["active"]    = True
                            _sunrise["start_ms"]  = now
                            _sunrise["dur_ms"]    = dur_ms
                            _sunrise["target_br"] = float(alarm.get("brightness", 1.0))
                            _sunrise["sunset"]    = False
                            engine.set_power(True)
                            engine.set_brightness(0.0)
                            print(f"[alarm] sunrise! {key[0]:02d}:{key[1]:02d}")
                        # echo flag: web-UI info only. Each board runs its own
                        # alarm — without the flag, an alarm targeting ONE
                        # board leaked its state onto the other board.
                        payload = engine.get_event_payload()
                        payload["echo"] = True
                        publish_event(payload)
                        break

        # ── Manual alarm test — same code path as a real alarm ──
        if _test_alarm is not None:
            _sunrise["active"]   = True
            _sunrise["start_ms"] = now
            _sunrise["dur_ms"]   = _test_alarm["dur_ms"]
            if _test_alarm["type"] == "sunset":
                _sunrise["target_br"] = 0.0
                _sunrise["sunset"]    = True
                _sunrise["start_br"]  = engine._brightness
            else:
                _sunrise["target_br"] = 1.0
                _sunrise["sunset"]    = False
                engine.set_power(True)
                engine.set_brightness(0.0)
            print(f"[alarm] test {_test_alarm['type']} started")
            _test_alarm = None
            payload = engine.get_event_payload()
            payload["echo"] = True
            publish_event(payload)

        # ── Sunrise / sunset ramp ──
        if _sunrise["active"]:
            elapsed = utime.ticks_diff(now, _sunrise["start_ms"])
            if elapsed >= _sunrise["dur_ms"]:
                if _sunrise.get("sunset"):
                    engine.set_power(False)
                    # Restore pre-sunset brightness so the next power-on
                    # doesn't come up at brightness 0 (invisible)
                    engine.set_brightness(_sunrise.get("start_br", LED_BRIGHTNESS))
                    save_state()
                else:
                    engine.set_brightness(_sunrise["target_br"])
                    engine.set_sunrise(1.0)   # land on clean warm white
                _sunrise["active"] = False
                payload = engine.get_event_payload()
                payload["echo"] = True
                publish_event(payload)
            else:
                progress = elapsed / _sunrise["dur_ms"]
                if _sunrise.get("sunset"):
                    start_br = _sunrise.get("start_br", engine._brightness)
                    engine.set_brightness(start_br * (1.0 - progress))
                    # Reverse the dawn: warm white deepens to ember as it dims
                    engine.set_sunrise(1.0 - progress)
                else:
                    engine.set_brightness(progress * _sunrise["target_br"])
                    # Ember red at dawn → warm white at full brightness
                    engine.set_sunrise(progress)

        # ── Idle drift — subtle nudge with slow fade, synced to other board ──
        if engine.check_drift():
            prev_steps = engine._fade_steps
            engine.set_fade_steps(SYNC_FADE_STEPS)  # use slow fade for drift
            engine.drift()
            engine.set_fade_steps(prev_steps)        # restore normal speed after
            publish_event(engine.get_event_payload())

        # ── Poll touch sensors ──
        event = touch.update()

        if event == "tap":
            print("[touch] tap")
            engine.impulse()
            publish_event(engine.get_event_payload())

        elif event == "hold":
            print("[touch] hold → toggle power")
            engine.toggle_power()
            publish_event(engine.get_event_payload())

        # ── Render frame at target FPS ──
        if utime.ticks_diff(now, last_frame) >= FRAME_MS:
            engine.tick(strip)
            last_frame = now

        utime.sleep_ms(1)


# ── Run ──────────────────────────────────────────────────────
# Top-level crash guard: any unhandled exception prints a traceback then
# reboots after 5 s so the board never hangs permanently on a firmware bug.
# boot.py runs on every reboot and pulls the latest code from GitHub, so
# a pushed fix is automatically picked up on the next crash-reboot cycle.
if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            sys.print_exception(e)
            print("[FATAL] unhandled exception — rebooting in 5 s")
            try:
                strip.off()
            except Exception:
                pass   # even the cleanup must not block the recovery reboot
            for _ in range(5):
                feed_wdt()
                utime.sleep(1)
            machine.reset()
