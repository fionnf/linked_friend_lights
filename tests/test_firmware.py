#!/usr/bin/env python3
"""
Firmware smoke test — actually RUNS main() against stubbed MicroPython
modules. `python -m py_compile` cannot catch runtime errors: a stray
function-level `import machine` once shadowed the module-level import for
the whole of main(), so arming the watchdog raised UnboundLocalError and
every board boot-looped. Only executing main() catches that class of bug.

Run from the repo root:   python3 tests/test_firmware.py
"""
import os, sys, json, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

def run():
    work = tempfile.mkdtemp(prefix="fwtest-")
    for f in ("main.py", "colour.py", "sk6812.py", "touch.py"):
        shutil.copy(os.path.join(ROOT, f), work)
    for item in os.listdir(os.path.join(HERE, "stubs")):
        src = os.path.join(HERE, "stubs", item)
        (shutil.copytree if os.path.isdir(src) else shutil.copy)(
            src, os.path.join(work, item))

    sys.path.insert(0, work)
    os.chdir(work)                      # state.json etc. land in the sandbox
    for m in list(sys.modules):
        if m.split(".")[0] in ("machine","utime","network","rp2","webrepl",
                               "ntptime","urandom","ujson","umqtt","config",
                               "main","colour","sk6812","touch"):
            del sys.modules[m]

    # gc.mem_free() is MicroPython-only; gc is a builtin so it cannot be
    # shadowed by a stub file — patch the attribute instead.
    import gc
    if not hasattr(gc, "mem_free"): gc.mem_free = lambda: 102400
    # sys.print_exception is MicroPython-only
    import traceback as _tb
    if not hasattr(sys, "print_exception"):
        sys.print_exception = lambda e, *a: _tb.print_exception(type(e), e, e.__traceback__)

    import machine, utime, umqtt.simple as us
    import main as fw

    failures = []
    def check(name, cond, detail=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
        if not cond: failures.append(name)

    # Stop the infinite main loop after N iterations by making the frame
    # sleep raise once we've seen enough of the loop.
    class Done(Exception): pass
    iters = [0]
    real_sleep_ms = utime.sleep_ms
    def counting_sleep(ms):
        real_sleep_ms(ms)
        iters[0] += 1
        if iters[0] > LIMIT: raise Done()
    utime.sleep_ms = counting_sleep
    fw.utime.sleep_ms = counting_sleep

    print("\n=== 1. boot + main loop ===")
    LIMIT = 3000                        # ~3 s of simulated loop
    try:
        fw.main()
        check("main() ran without crashing", False, "returned unexpectedly")
    except Done:
        check("main() boots and runs the loop (no UnboundLocalError)", True)
    except machine.ResetCalled:
        check("main() booted without an unexpected reset", False)
    except Exception as e:
        check(f"main() boots cleanly", False, f"{type(e).__name__}: {e}")
        import traceback; traceback.print_exc()

    check("hardware watchdog armed", fw.wdt is not None)
    check("watchdog is being fed", fw.wdt and fw.wdt.feeds > 10,
          f"feeds={fw.wdt.feeds if fw.wdt else 0}")
    check("MQTT client connected", fw.client is not None)

    subs = [s.decode() for s in fw.client.subs]
    check("subscribed to events topic", "test_prefix/events" in subs, str(subs))
    check("subscribed to RETAINED ALARMS topic", "test_prefix/alarms" in subs, str(subs))
    check("Last-Will registered", fw.client.will is not None)

    def _s(x): return x.decode() if isinstance(x, bytes) else x
    topics = [_s(t) for t, m, r in us.published]
    check("published retained presence", any("status/board_a" in t for t in topics))
    hb = [json.loads(_s(m)) for t, m, r in us.published if "heartbeat" in _s(m)]
    check("published a heartbeat", len(hb) > 0)
    check("boot asks the partner for its state",
          any("state_request" in _s(m) for t, m, r in us.published))
    if hb:
        check("heartbeat reports ntp + alarms", "ntp" in hb[0] and "alarms" in hb[0], str(hb[0]))

    print("\n=== 2. reboot command (the button) ===")
    us.published.clear()
    fw.client.inject(b"test_prefix/events",
                     json.dumps({"from": "web_app", "reboot": True}).encode())
    before = machine.reset_count[0]
    try:
        fw.client.check_msg()
    except machine.ResetCalled:
        pass
    check("reboot message triggers machine.reset()", machine.reset_count[0] == before + 1)
    check("state saved before rebooting", os.path.exists(os.path.join(work, "state.json")))

    print("\n=== 3. retained alarm schedule ===")
    fw.client.inject(b"test_prefix/alarms",
                     json.dumps([{"enabled": True, "type": "sunrise", "hour": 5,
                                  "minute": 0, "days": [0,1,2,3,4],
                                  "boards": ["board_a","board_b"]}]).encode())
    fw.client.check_msg()
    check("retained array installs the schedule", len(fw._alarms) == 1, str(fw._alarms))

    print("\n=== 4. hostile input cannot crash the handler ===")
    ok = True
    for bad in [b"not json", b"null", b"[1,2,3]", b'{"groups":"x"}',
                b'{"brightness":"NaN"}', b'{"set_alarms":42}', b"\xff\xfe", b"{}"]:
        try:
            fw.client.inject(b"test_prefix/events", bad); fw.client.check_msg()
        except machine.ResetCalled:
            pass
        except Exception as e:
            ok = False; print(f"    raised on {bad!r}: {type(e).__name__}: {e}")
    check("malformed messages never raise", ok)

    print("\n=== 5. commands apply ===")
    fw.client.inject(b"test_prefix/events", json.dumps({
        "from": "web_app", "on": True, "brightness": 0.42, "fade_steps": 120,
        "groups": [{"pos": 0.5, "w": 0.5, "size": 4},
                   {"pos": 0.9, "w": 0.1, "size": 6}]}).encode())
    fw.client.check_msg()
    check("brightness applied", abs(fw.engine._brightness - 0.42) < 1e-6,
          str(fw.engine._brightness))
    check("fade_steps applied", fw.engine._fade_steps == 120, str(fw.engine._fade_steps))
    check("groups cover the whole strip", sum(fw.engine._group_sizes) == 10,
          str(fw.engine._group_sizes))

    print("\n=== 6. flash wear ===")
    # The web app puts `on` in every state publish, so a slider drag sends
    # ~8 messages/s. Each one used to rewrite state.json — a flash
    # erase/write cycle that also stalls the render loop.
    import os as _os
    st = _os.path.join(work, "state.json")
    def mtime_writes(fn, n):
        seen = 0
        for i in range(n):
            before = _os.stat(st).st_mtime_ns if _os.path.exists(st) else -1
            fn(i)
            after = _os.stat(st).st_mtime_ns if _os.path.exists(st) else -1
            if after != before: seen += 1
        return seen

    def drag(i):
        fw.client.inject(b"test_prefix/events", json.dumps({
            "from": "web_app", "on": True, "brightness": 0.30 + i * 0.01}).encode())
        fw.client.check_msg()
    writes = mtime_writes(drag, 40)
    check("slider drag does not rewrite flash", writes == 0, f"{writes} writes in 40 msgs")

    def toggle(i):
        fw.client.inject(b"test_prefix/events", json.dumps({
            "from": "web_app", "on": bool(i % 2)}).encode())
        fw.client.check_msg()
    writes = mtime_writes(toggle, 6)
    check("real power toggles DO persist", writes == 6, f"{writes}/6 persisted")

    # repeating the same power state must not rewrite
    fw.client.inject(b"test_prefix/events", json.dumps({"from":"web_app","on":True}).encode())
    fw.client.check_msg()
    writes = mtime_writes(lambda i: (
        fw.client.inject(b"test_prefix/events", json.dumps({"from":"web_app","on":True}).encode()),
        fw.client.check_msg()), 10)
    check("repeated identical state is a no-op", writes == 0, f"{writes} redundant writes")

    print("\n=== 7. per-lamp targeting sticks ===")
    # The BOSS re-publishes state every 60 s; that used to overwrite a lamp
    # the user had deliberately set on its own ("Send to -> LS" reverted).
    fw._independent_until = None
    fw.client.inject(b"test_prefix/events", json.dumps({
        "from": "web_app", "target": "board_b",
        "groups": [{"pos": 0.8, "w": 0.2, "size": 10}]}).encode())
    fw.client.check_msg()
    check("targeted command unlinks the lamps", fw._independent_until is not None)

    fw.client.inject(b"test_prefix/events", json.dumps({
        "from": "web_app",
        "groups": [{"pos": 0.1, "w": 1.0, "size": 10}]}).encode())
    fw.client.check_msg()
    check("broadcast command re-links them", fw._independent_until is None)

    print("\n=== 8. test_alarm ===")
    fw._test_alarm = None
    fw.client.inject(b"test_prefix/events", json.dumps({
        "from": "web_app", "test_alarm": "sunrise", "test_seconds": 20}).encode())
    fw.client.check_msg()
    check("test_alarm queued for the main loop", fw._test_alarm is not None,
          str(fw._test_alarm))

    print("\n=== 9. colour survives a reboot ===")
    # Every reboot (nightly OTA, offline watchdog, crash, hang) used to bring
    # a lamp back in warm white, and the BOSS spread that white to the
    # follower within a minute.
    fw.client.inject(b"test_prefix/events", json.dumps({
        "from": "web_app", "on": True,
        "groups": [{"pos": 0.5, "w": 0.5, "size": 4},
                   {"pos": 0.9, "w": 0.1, "size": 6}]}).encode())
    fw.client.check_msg()
    check("a colour command schedules a write rather than making one", fw._colour_save_at is not None)
    before = _os.stat(st).st_mtime_ns if _os.path.exists(st) else -1
    fw.flush_colour(utime.ticks_ms())
    check("the write waits for the debounce", (_os.stat(st).st_mtime_ns if _os.path.exists(st) else -1) == before)
    utime.advance(fw.COLOUR_SAVE_DELAY_MS + 1)
    fw.flush_colour(utime.ticks_ms())
    saved = json.load(open(st))
    check("the colour lands in state.json after the debounce",
          [round(g["pos"], 4) for g in saved.get("groups", [])] == [0.5, 0.9], str(saved))
    check("no second write while nothing changed", fw._colour_save_at is None)

    # Reboot: a fresh engine knows only warm white, then restore_state runs
    fw.engine.force_colour([{"pos": 0.0, "w": 1.0, "size": 10}], 1)
    fw._colour_known = False
    fw._saved_state = None
    fw.restore_state()
    check("restore puts the colour back", [round(p, 4) for p in fw.engine._target_pos] == [0.5, 0.9],
          str(fw.engine._target_pos))
    check("restore keeps the group layout", fw.engine._group_sizes == [4, 6], str(fw.engine._group_sizes))
    check("a restored colour counts as known", fw._colour_known)

    print("\n=== 10. the BOSS never syncs a colour it was not given ===")
    now = utime.ticks_ms()
    fw._independent_until = None
    fw._colour_known = False
    fw._sync_at = now - 1
    us.published.clear()
    fw.boss_sync(now)
    check("fresh boot: no sync", not any("sync" in _s(m) for t, m, r in us.published))
    fw._colour_known = True
    fw._sync_at = now - 1
    fw.boss_sync(now)
    synced = [json.loads(_s(m)) for t, m, r in us.published if '"sync"' in _s(m)]
    check("known colour: sync goes out", len(synced) == 1 and synced[0].get("sync") is True
          and [round(g["pos"], 4) for g in synced[0]["groups"]] == [0.5, 0.9], str(synced))

    print("\n=== 11. a partner that reconnects is answered ===")
    us.published.clear()
    fw._independent_until = None
    fw.client.inject(b"test_prefix/events", json.dumps({"from": "board_b", "state_request": True}).encode())
    fw.client.check_msg()
    answers = [json.loads(_s(m)) for t, m, r in us.published if "state_request" not in _s(m)]
    check("linked: the request is answered with sync", len(answers) == 1 and answers[0].get("sync") is True
          and "groups" in answers[0], str(answers))
    us.published.clear()
    fw._independent_until = utime.ticks_add(utime.ticks_ms(), 60_000)
    fw.client.inject(b"test_prefix/events", json.dumps({"from": "board_b", "state_request": True}).encode())
    fw.client.check_msg()
    check("set apart: the request is left alone", len(us.published) == 0, str(us.published))
    fw._independent_until = None

    print("\n=== 12. the partner's answer is adopted with the slow fade ===")
    fw.client.inject(b"test_prefix/events", json.dumps({
        "from": "board_b", "sync": True, "on": True,
        "groups": [{"pos": 0.25, "w": 0.5, "size": 10}]}).encode())
    fw.client.check_msg()
    check("partner state applied", abs(fw.engine._target_pos[0] - 0.25) < 1e-6, str(fw.engine._target_pos))
    check("with the 300-step sync fade", fw.engine._fade_steps_per[0] == fw.SYNC_FADE_STEPS,
          str(fw.engine._fade_steps_per))
    check("and scheduled for flash", fw._colour_save_at is not None)

    print("\n=== 13. the daily update runs at 13:00 Zurich time ===")
    check("13 July 2026 (CEST): 11:00 UTC is 13:00", fw.zurich_hour((2026, 7, 13, 11, 0, 0, 0, 194)) == 13)
    check("13 January 2026 (CET): 12:00 UTC is 13:00", fw.zurich_hour((2026, 1, 13, 12, 0, 0, 1, 13)) == 13)
    check("last Sunday of March 2026 is the 29th", fw._last_sunday(2026, 3) == 29)
    check("last Sunday of October 2026 is the 25th", fw._last_sunday(2026, 10) == 25)
    check("switch-over: 25 Oct 2026 00:59 UTC is still summer time", fw.zurich_offset_hours((2026, 10, 25, 0, 59, 0, 6, 298)) == 2)
    check("switch-over: 25 Oct 2026 01:00 UTC is winter time", fw.zurich_offset_hours((2026, 10, 25, 1, 0, 0, 6, 298)) == 1)
    check("switch-over: 29 Mar 2026 01:00 UTC is summer time", fw.zurich_offset_hours((2026, 3, 29, 1, 0, 0, 6, 88)) == 2)
    check("OTA hour is 13 local", fw.OTA_HOUR_LOCAL == 13)
    check("the offline reboot is gone", not hasattr(fw, "WIFI_OFFLINE_REBOOT_MS"))

    print("\n=== 14. a silent link is dropped, not endured ===")
    now = utime.ticks_ms()
    fw._last_rx_ms = now
    check("fresh traffic: alive", fw.link_alive(now))
    check("quiet for a while: still alive", fw.link_alive(now + fw.RX_SILENCE_MS - 1))
    check("silent past the limit: dead", not fw.link_alive(now + fw.RX_SILENCE_MS))
    fw.client.inject(b"test_prefix/events", json.dumps({"from": "board_a", "heartbeat": True}).encode())
    utime.advance(fw.RX_SILENCE_MS + 5)
    fw.client.check_msg()
    check("the board's own echo counts as a sign of life", fw.link_alive(utime.ticks_ms()))
    check("the broker handshake is bounded", fw.client.timeout == fw.MQTT_SOCKET_TIMEOUT_S, str(fw.client.timeout))

    shutil.rmtree(work, ignore_errors=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures)); return 1
    print("ALL FIRMWARE SMOKE TESTS PASSED"); return 0

if __name__ == "__main__":
    sys.exit(run())
