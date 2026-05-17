# ============================================================
#  main.py  —  Entry point, runs on BOTH boards identically
#  Only config.py differs between boards.
# ============================================================

import math
import utime
import ujson
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

FRAME_MS         = 16       # ~60 fps tick rate
SYNC_INTERVAL_MS = 60_000   # boss resyncs follower every 60 s
SYNC_FADE_STEPS  = 300      # ~5 s slow fade on follower when resyncing
from sk6812 import SK6812
from touch   import TouchManager
from colour  import ColourEngine

# ── MQTT topics ─────────────────────────────────────────────
# All boards publish to and subscribe from the same shared topic.
# They ignore messages that carry their own BOARD_ID (echo prevention).
TOPIC_EVENTS = (MQTT_TOPIC_PREFIX + "/events").encode()
TOPIC_POWER_SET = (MQTT_TOPIC_PREFIX + "/power/set").encode()
TOPIC_POWER_STATE = (MQTT_TOPIC_PREFIX + "/power/state").encode()
TOPIC_HA_SWITCH_CONFIG = ("homeassistant/switch/" + MQTT_TOPIC_PREFIX + "_light/config").encode()

# ── Module-level objects ─────────────────────────────────────
strip  = SK6812(pin=LED_PIN, num_leds=NUM_LEDS, brightness=LED_BRIGHTNESS)
touch  = TouchManager(TOUCH_PINS)
engine = ColourEngine()
client = None   # MQTTClient, initialised after WiFi

# ── WiFi ─────────────────────────────────────────────────────

NETWORKS_FILE = "networks.json"

def load_extra_networks():
    """Load user-added networks from networks.json."""
    try:
        with open(NETWORKS_FILE) as f:
            return ujson.load(f)
    except Exception:
        return []

def save_network(ssid, password):
    """Append a new network to networks.json, avoiding duplicates."""
    nets = load_extra_networks()
    if any(n[0] == ssid for n in nets):
        print(f"[wifi] {ssid} already saved")
        return
    nets.append([ssid, password])
    with open(NETWORKS_FILE, "w") as f:
        ujson.dump(nets, f)
    print(f"[wifi] saved new network: {ssid}")

def all_networks():
    return WIFI_NETWORKS + load_extra_networks()

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

def publish_power_state():
    """Publish retained ON/OFF state for assistant integrations."""
    global client
    if client is None:
        return
    try:
        state = b"ON" if engine._powered_on else b"OFF"
        client.publish(TOPIC_POWER_STATE, state, retain=True)
    except Exception as e:
        print(f"[mqtt] power state publish error: {e}")
        client = None

def publish_ha_switch_discovery():
    """Publish Home Assistant MQTT discovery for a simple on/off light switch."""
    global client
    if client is None:
        return
    payload = {
        "name": "Linked Friend Lights",
        "unique_id": MQTT_TOPIC_PREFIX + "_light",
        "command_topic": MQTT_TOPIC_PREFIX + "/power/set",
        "state_topic": MQTT_TOPIC_PREFIX + "/power/state",
        "payload_on": "ON",
        "payload_off": "OFF",
        "icon": "mdi:lightbulb-group",
    }
    try:
        client.publish(TOPIC_HA_SWITCH_CONFIG, ujson.dumps(payload), retain=True)
    except Exception as e:
        print(f"[mqtt] discovery publish error: {e}")
        client = None

def on_message(topic, msg):
    """Called by the MQTT library when a message arrives."""
    if topic == TOPIC_POWER_SET:
        try:
            cmd = msg.decode("utf-8", "ignore").strip().lower()
        except Exception:
            return
        if cmd in ("on", "1", "true"):
            engine.set_power(True)
        elif cmd in ("off", "0", "false"):
            engine.set_power(False)
        else:
            print(f"[mqtt] ignored power cmd: {cmd}")
            return
        publish_event({"on": engine._powered_on})
        return

    try:
        data = ujson.loads(msg)
    except Exception:
        return

    # Ignore our own echoed messages
    if data.get("from") == BOARD_ID:
        return

    # Ignore messages targeted at a different board
    target = data.get("target")
    if target is not None and target != BOARD_ID:
        return

    print(f"[mqtt] recv: {data}")
    groups        = data.get("groups")
    on            = data.get("on")
    brightness    = data.get("brightness")
    reverse       = data.get("reverse")
    fade_steps     = data.get("fade_steps")
    drift_enabled  = data.get("drift_enabled")
    drift_interval = data.get("drift_interval")
    anim_mode      = data.get("anim_mode", "__unset__")
    anim_speed     = data.get("anim_speed")
    anim_params    = data.get("anim_params")

    if on is not None:
        engine.set_power(bool(on))
        publish_power_state()
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
    if anim_mode != "__unset__":
        engine.set_animation(anim_mode, anim_speed if anim_speed is not None else 1.0, anim_params)
    if groups is not None and on is not False and (not engine._anim_mode or not data.get("sync")):
        fade_override = SYNC_FADE_STEPS if data.get("sync") else None
        engine.force_colour(groups, fade_steps_override=fade_override)

    add_net = data.get("add_network")
    if add_net:
        save_network(add_net["ssid"], add_net["password"])

    if "set_alarms" in data:
        save_alarms(data["set_alarms"])
        print(f"[alarm] saved {len(_alarms)} alarms")

    if data.get("reboot"):
        print("[mqtt] reboot requested — saving state and rebooting")
        save_state()
        import machine
        machine.reset()


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
    c.connect()
    c.subscribe(TOPIC_EVENTS)
    c.subscribe(TOPIC_POWER_SET)
    print(f"[mqtt] connected — client_id={client_id}, topic={TOPIC_EVENTS}")
    global client
    client = c
    publish_ha_switch_discovery()
    publish_power_state()
    return c


def publish_event(payload: dict):
    """Publish a colour/power event to the shared topic."""
    global client
    if client is None:
        return
    payload["from"] = BOARD_ID
    try:
        client.publish(TOPIC_EVENTS, ujson.dumps(payload))
        if "on" in payload:
            publish_power_state()
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
            _alarms = ujson.load(f)
    except Exception:
        _alarms = []

def save_alarms(data):
    global _alarms
    _alarms = data
    try:
        with open(ALARM_FILE, "w") as f:
            ujson.dump(data, f)
    except Exception as e:
        print(f"[alarm] save failed: {e}")

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            ujson.dump({"on": engine._powered_on}, f)
    except Exception as e:
        print(f"[state] save failed: {e}")

def restore_state():
    try:
        with open(STATE_FILE) as f:
            d = ujson.load(f)
        if not d.get("on", True):
            engine.set_power(False)
            engine._power_level = 0.0  # instant off, no fade-in
            print("[state] restored: off")
        else:
            print("[state] restored: on")
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
    global client

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
    _sunrise = {"active": False, "start_ms": 0, "dur_ms": 0, "target_br": 1.0}
    _alarm_fired = set()  # (hour, minute) pairs fired this calendar day
    last_frame      = utime.ticks_ms()
    _reconnect_at   = utime.ticks_add(utime.ticks_ms(), RECONNECT_DELAY_MS)
    _ping_at        = utime.ticks_add(utime.ticks_ms(), 20_000)
    _sync_at        = utime.ticks_add(utime.ticks_ms(), SYNC_INTERVAL_MS)
    _heartbeat_at   = utime.ticks_add(utime.ticks_ms(), 5_000)  # first beat soon after boot
    _backoff_ms     = RECONNECT_DELAY_MS

    while True:
        now = utime.ticks_ms()

        # ── Reconnect MQTT with exponential backoff ──
        if client is None and utime.ticks_diff(now, _reconnect_at) >= 0:
            try:
                client = connect_mqtt()
                _backoff_ms = RECONNECT_DELAY_MS   # reset on success
                _ping_at    = utime.ticks_add(now, 20_000)
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

        # ── Boss sync — publish current state every 60 s for follower to drift to ──
        if BOSS and client is not None and utime.ticks_diff(now, _sync_at) >= 0:
            payload = engine.get_event_payload()
            payload["sync"] = True
            publish_event(payload)
            print("[sync] boss published state")
            _sync_at = utime.ticks_add(now, SYNC_INTERVAL_MS)

        # ── Heartbeat — announce presence every 30 s ──
        if client is not None and utime.ticks_diff(now, _heartbeat_at) >= 0:
            publish_event({"heartbeat": True})
            _heartbeat_at = utime.ticks_add(now, 30_000)

        # ── Daily OTA reboot at OTA_HOUR_UTC — save state first ──
        if ntp_ok:
            t = utime.localtime()
            cur_min = t[4]  # minute within hour
            if t[3] == OTA_HOUR_UTC and cur_min != _ota_check_min:
                _ota_check_min = cur_min
                if cur_min == 0:
                    print("[ota] 5 pm UTC — saving state and rebooting for OTA")
                    save_state()
                    import machine
                    machine.reset()

        # ── Sunrise alarm check ──
        if ntp_ok and _alarms:
            t = utime.localtime()
            cur_min = t[4]
            if cur_min != _alarm_checked_min:
                _alarm_checked_min = cur_min
                # Reset fired set at midnight
                if t[3] == 0 and cur_min == 0:
                    _alarm_fired.clear()
                for alarm in _alarms:
                    key = (alarm.get("hour", 0), alarm.get("minute", 0))
                    boards = alarm.get("boards", [])
                    if (alarm.get("enabled", True)
                            and t[3] == key[0] and cur_min == key[1]
                            and t[6] in alarm.get("days", list(range(7)))
                            and key not in _alarm_fired
                            and (not boards or BOARD_ID in boards)):
                        _alarm_fired.add(key)
                        dur_ms = int(alarm.get("duration_min", 30)) * 60_000
                        _sunrise["active"]    = True
                        _sunrise["start_ms"]  = now
                        _sunrise["dur_ms"]    = dur_ms
                        _sunrise["target_br"] = float(alarm.get("brightness", 1.0))
                        engine.set_power(True)
                        engine.set_brightness(0.0)
                        print(f"[alarm] sunrise! {key[0]:02d}:{key[1]:02d}")
                        publish_event(engine.get_event_payload())
                        break

        # ── Sunrise ramp ──
        if _sunrise["active"]:
            elapsed = utime.ticks_diff(now, _sunrise["start_ms"])
            if elapsed >= _sunrise["dur_ms"]:
                engine.set_brightness(_sunrise["target_br"])
                _sunrise["active"] = False
                publish_event(engine.get_event_payload())
            else:
                progress = elapsed / _sunrise["dur_ms"]
                engine.set_brightness(progress * _sunrise["target_br"])

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
if __name__ == "__main__":
    main()
