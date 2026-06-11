# ============================================================
#  boot.py — OTA updater
#  Runs before main.py. Downloads latest code from GitHub,
#  then MicroPython continues to main.py automatically.
#  config.py is never touched — it stays per-board.
# ============================================================

import network
import utime

# ── Files to sync from GitHub ────────────────────────────────
REPO_RAW = "https://raw.githubusercontent.com/fionnf/linked_friend_lights/master/"
SYNC_FILES = ["main.py", "colour.py", "sk6812.py", "touch.py"]

# ── Connect WiFi (mirrors main.py logic, standalone) ─────────
def _load_networks():
    try:
        from config import WIFI_NETWORKS
    except Exception:
        WIFI_NETWORKS = []
    extra = []
    try:
        import ujson
        with open("networks.json") as f:
            extra = ujson.load(f)
    except Exception:
        pass
    return WIFI_NETWORKS + extra

def _connect():
    networks = _load_networks()
    if not networks:
        print("[ota] no networks configured — skipping update")
        return False

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return True

    try:
        visible = {ap[0].decode("utf-8", "ignore") for ap in wlan.scan()}
    except Exception:
        visible = set()

    for ssid, password in networks:
        if visible and ssid not in visible:
            continue
        print(f"[ota] connecting to {ssid}...")
        wlan.connect(ssid, password)
        deadline = utime.ticks_add(utime.ticks_ms(), 12_000)
        while not wlan.isconnected():
            if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
                wlan.disconnect()
                utime.sleep_ms(1000)
                break
            utime.sleep_ms(200)
        if wlan.isconnected():
            print(f"[ota] connected — IP {wlan.ifconfig()[0]}")
            return True

    print("[ota] no WiFi — skipping update, booting with existing files")
    return False


def _update():
    try:
        import urequests
    except ImportError:
        print("[ota] urequests not available — skipping update")
        return

    updated = []
    for fname in SYNC_FILES:
        url = REPO_RAW + fname
        try:
            r = urequests.get(url, timeout=10)
            if r.status_code == 200:
                content = r.text
                r.close()
                # Compare with existing file to avoid unnecessary writes
                try:
                    with open(fname, "r") as f:
                        existing = f.read()
                    if existing == content:
                        continue
                except OSError:
                    pass  # file doesn't exist yet
                with open(fname, "w") as f:
                    f.write(content)
                updated.append(fname)
                print(f"[ota] updated {fname}")
            else:
                print(f"[ota] {fname} — HTTP {r.status_code}, keeping existing")
                r.close()
        except Exception as e:
            print(f"[ota] {fname} failed: {e}, keeping existing")

    if not updated:
        print("[ota] all files up to date")
    else:
        print(f"[ota] updated: {updated}")


# ── Run ───────────────────────────────────────────────────────
print("[ota] checking for updates...")
if _connect():
    _update()
print("[ota] booting...")
