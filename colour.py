# ============================================================
#  colour.py  —  Organic colour engine
# ============================================================
import math
import urandom
import utime
from config import (
    BASE_WARM_WHITE, TINT_PALETTE,
    HUE_SHIFT_MIN, HUE_SHIFT_MAX,
    FADE_STEPS,
    BREATHE_SPEED, BREATHE_DEPTH,
    IDLE_DRIFT_INTERVAL_S,
    NUM_LEDS, LED_BRIGHTNESS,
    NUM_GROUPS, GROUP_MIN_LEDS, GROUP_MAX_LEDS,
)
from config import REVERSE_LEDS

# ── Helpers ──────────────────────────────────────────────────

def _lerp(a, b, t):
    return a + (b - a) * t

def _lerp_colour(c1, c2, t):
    return tuple(int(_lerp(a, b, t)) for a, b in zip(c1, c2))

def _rand_float(lo, hi):
    return lo + (urandom.getrandbits(16) / 65536.0) * (hi - lo)

def _random_partition(total, n, mn, mx):
    mn = max(1, mn)
    mx = min(mx, total - (n - 1) * mn)
    sizes = []
    remaining = total
    for i in range(n):
        left = n - i
        lo = max(mn, remaining - left * mx)
        hi = min(mx, remaining - (left - 1) * mn)
        if lo > hi:
            lo = hi = remaining - (left - 1) * mn
        size = int(_rand_float(lo, hi + 1))
        size = max(mn, min(mx, size))
        sizes.append(size)
        remaining -= size
    sizes[-1] += remaining
    return sizes

def _palette_colour(position):
    n = len(TINT_PALETTE)
    scaled = position * (n - 1)
    idx    = int(scaled)
    frac   = scaled - idx
    if idx >= n - 1:
        tint_rgb = TINT_PALETTE[-1]
    else:
        c1, c2   = TINT_PALETTE[idx], TINT_PALETTE[idx + 1]
        tint_rgb = tuple(int(_lerp(a, b, frac)) for a, b in zip(c1, c2))
    sat = position
    r = int(_lerp(BASE_WARM_WHITE[0], tint_rgb[0], sat))
    g = int(_lerp(BASE_WARM_WHITE[1], tint_rgb[1], sat))
    b = int(_lerp(BASE_WARM_WHITE[2], tint_rgb[2], sat))
    w = int(BASE_WARM_WHITE[3] * (1.0 - sat))
    return (r, g, b, w)


# ── ColourEngine ─────────────────────────────────────────────

class ColourEngine:

    def __init__(self):
        n = NUM_GROUPS
        self._n             = n
        self._pos           = [0.0] * n
        self._target_pos    = [0.0] * n
        self._colour        = [BASE_WARM_WHITE] * n
        self._target_col    = [BASE_WARM_WHITE] * n
        self._fade_step     = [0] * n
        self._fading        = [False] * n
        self._w_level       = [1.0] * n
        self._w_target      = [1.0] * n
        self._w_step        = [0] * n
        self._w_fading      = [False] * n
        self._breathe_phase = [i * (6.28 / n) for i in range(n)]
        self._group_sizes   = _random_partition(NUM_LEDS, n, GROUP_MIN_LEDS, GROUP_MAX_LEDS)

        self._fade_steps_per = [FADE_STEPS] * n   # per-group step target

        self._powered_on    = True
        self._power_level   = 1.0
        self._power_dir     = 0
        self._brightness    = LED_BRIGHTNESS
        self._reverse       = REVERSE_LEDS
        self._fade_steps    = FADE_STEPS      # runtime-adjustable
        self._drift_enabled  = True

        self._drift_interval = IDLE_DRIFT_INTERVAL_S
        self._last_drift     = utime.time()
        self._time_ms       = utime.ticks_ms()

    # ── Public controls ─────────────────────────────────────

    def impulse(self):
        if not self._powered_on:
            return
        self._group_sizes = _random_partition(NUM_LEDS, self._n, GROUP_MIN_LEDS, GROUP_MAX_LEDS)
        for i in range(self._n):
            self._start_fade(i, _rand_float(0.0, 1.0))
            self._w_target[i] = _rand_float(0.6, 1.0)
            self._w_step[i]   = 0
            self._w_fading[i] = True

    def toggle_power(self):
        self._powered_on = not self._powered_on
        self._power_dir  = 1 if self._powered_on else -1

    def force_colour(self, groups, fade_steps_override=None):
        if not self._powered_on:
            return
        fs = fade_steps_override or self._fade_steps
        n  = len(groups)
        # Dynamically resize per-group arrays so any number of groups works
        # (e.g. per-LED Static Rainbow sends NUM_LEDS groups)
        if n != self._n:
            def _resize(lst, default):
                while len(lst) < n: lst.append(default)
                del lst[n:]
            _resize(self._pos,           0.0)
            _resize(self._target_pos,    0.0)
            _resize(self._colour,        BASE_WARM_WHITE)
            _resize(self._target_col,    BASE_WARM_WHITE)
            _resize(self._fade_step,     0)
            _resize(self._fading,        False)
            _resize(self._w_level,       1.0)
            _resize(self._w_target,      1.0)
            _resize(self._w_step,        0)
            _resize(self._w_fading,      False)
            _resize(self._breathe_phase,  0.0)
            _resize(self._fade_steps_per, self._fade_steps)
            self._n = n
        self._group_sizes = []
        for i, g in enumerate(groups):
            self._group_sizes.append(g["size"])
            self._start_fade(i, g["pos"], fs)
            self._w_target[i] = g["w"]
            self._w_step[i]   = 0
            self._w_fading[i] = True

    def set_power(self, on):
        if on != self._powered_on:
            self.toggle_power()

    def set_brightness(self, brightness):
        self._brightness = max(0.0, min(1.0, brightness))

    def set_reverse(self, reverse):
        self._reverse = reverse

    def set_fade_steps(self, steps):
        self._fade_steps = max(1, int(steps))

    def set_drift_enabled(self, enabled):
        self._drift_enabled = bool(enabled)
        self._last_drift = utime.time()

    def set_drift_interval(self, seconds):
        self._drift_interval = max(5, int(seconds))

    def check_drift(self):
        if self._drift_enabled and utime.time() - self._last_drift > self._drift_interval:
            self._last_drift = utime.time()
            return True
        return False

    def drift(self):
        """Subtle autonomous drift — tiny hue nudges, occasional ±1 LED group resize."""
        if not self._powered_on:
            return
        for i in range(self._n):
            # Tiny hue shift — 1–5% of the palette, same direction tendency
            shift = _rand_float(0.01, 0.05)
            direction = 1 if urandom.getrandbits(1) else -1
            new_pos = max(0.0, min(1.0, self._pos[i] + direction * shift))
            self._start_fade(i, new_pos)
            # Small W nudge — stay within ±8% of current level
            new_w = max(0.6, min(1.0, self._w_level[i] + _rand_float(-0.08, 0.08)))
            self._w_target[i] = new_w
            self._w_step[i]   = 0
            self._w_fading[i] = True

        # Occasionally shift one random group by ±1 LED
        if urandom.getrandbits(2) == 0:  # ~25% chance per drift event
            i = int(_rand_float(0, self._n))
            j = i - 1 if i == self._n - 1 else i + 1
            change = 1 if urandom.getrandbits(1) else -1
            if self._group_sizes[i] - change >= 1 and self._group_sizes[j] + change >= 1:
                self._group_sizes[i] -= change
                self._group_sizes[j] += change

    # ── Tick ─────────────────────────────────────────────────

    def tick(self, strip):
        now = utime.ticks_ms()
        dt  = utime.ticks_diff(now, self._time_ms)
        self._time_ms = now

        # Power fade
        if self._power_dir != 0:
            self._power_level += self._power_dir * (dt / 800.0)
            if self._power_level >= 1.0:
                self._power_level = 1.0
                self._power_dir   = 0
            elif self._power_level <= 0.0:
                self._power_level = 0.0
                self._power_dir   = 0
                strip.off()
                return

        if not self._powered_on and self._power_level == 0.0:
            strip.off()
            return

        strip.set_brightness(self._brightness)
        cursor = 0

        for i in range(self._n):
            fs = self._fade_steps_per[i]

            if self._fading[i]:
                t = self._fade_step[i] / fs
                self._colour[i]    = _lerp_colour(self._colour[i], self._target_col[i], t)
                self._fade_step[i] += 1
                if self._fade_step[i] >= fs:
                    self._fading[i] = False
                    self._colour[i] = self._target_col[i]
                    self._pos[i]    = self._target_pos[i]

            # W-level fade
            if self._w_fading[i]:
                t = self._w_step[i] / fs
                self._w_level[i] = _lerp(self._w_level[i], self._w_target[i], t)
                self._w_step[i] += 1
                if self._w_step[i] >= fs:
                    self._w_fading[i] = False
                    self._w_level[i]  = self._w_target[i]

            # Breathing
            self._breathe_phase[i] += BREATHE_SPEED * dt
            breath = 1.0 + math.sin(self._breathe_phase[i]) * BREATHE_DEPTH

            scale = breath * self._power_level

            r, g, b, w = self._colour[i]
            w = int(w * self._w_level[i])

            r = min(255, int(r * scale))
            g = min(255, int(g * scale))
            b = min(255, int(b * scale))
            w = min(255, int(w * scale))

            group_end = cursor + self._group_sizes[i]
            for j in range(cursor, group_end):
                idx = (NUM_LEDS - 1 - j) if self._reverse else j
                strip.set(idx, r, g, b, w)
            cursor = group_end

        strip.show()

    # ── MQTT payload ────────────────────────────────────────

    def get_event_payload(self):
        groups = [
            {"pos":  round(self._target_pos[i], 4),
             "w":    round(self._w_target[i], 3),
             "size": self._group_sizes[i]}
            for i in range(self._n)
        ]
        return {
            "groups":         groups,
            "on":             self._powered_on,
            "brightness":     round(self._brightness, 3),
            "fade_steps":     self._fade_steps,
            "drift_enabled":  self._drift_enabled,
            "drift_interval": self._drift_interval,
        }

    # ── Internal ─────────────────────────────────────────────

    def _start_fade(self, group, new_pos, steps=None):
        self._target_pos[group] = new_pos
        self._target_col[group] = _palette_colour(new_pos)
        self._fade_steps_per[group] = steps if steps is not None else self._fade_steps
        self._fade_step[group]  = 0
        self._fading[group]     = True
