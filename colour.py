# ============================================================
#  colour.py  —  Organic colour engine
# ============================================================
import math
import urandom
import utime
from config import (
    BASE_WARM_WHITE, TINT_PALETTE,
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

def _ease(t):
    """Smoothstep — symmetric ease-in-out, zero velocity at both ends.
    Makes a fade start and finish imperceptibly instead of stepping."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)

def _lerp_colour(c1, c2, t):
    return tuple(int(_lerp(a, b, t)) for a, b in zip(c1, c2))

def _rand_float(lo, hi):
    return lo + (urandom.getrandbits(16) / 65535.0) * (hi - lo)

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


# ── Sunrise gradient ─────────────────────────────────────────
# Explicit RGBW keyframes, deliberately NOT taken from TINT_PALETTE: there
# `pos` sets hue and saturation together, so anything warm is also nearly
# white and a dawn would be an invisible tint. Sweep: ember red → amber →
# golden → warm white. Brightness is ramped separately on top, so the early
# stops are seen dim and deep, exactly like a real dawn.
#
# Stops are evenly spaced by index, so *where* the stops sit is what shapes
# the curve. Weighting them toward the red end is what makes the dawn open
# deeper and hold there: the old middle stops now land at 0.6 and 0.8 rather
# than 0.33 and 0.66, so the white LED stays dark for the first three fifths
# and only creeps in near the end. Red never leaves — R is 255 the whole way,
# so the landing is a warm white with red still under it, not a cold white.
SUNRISE_STOPS = (
    (255,   6,   0,   0),   # 0.00  deepest ember — almost pure red
    (255,  18,   0,   0),   # 0.20  ember red
    (255,  45,   0,   0),   # 0.40  red, barely turning
    (255,  85,   0,   0),   # 0.60  deep orange
    (255, 145,  25,  55),   # 0.80  amber, white just creeping in
    (255, 190, 110, 190),   # 1.00  warm white
)

def sunrise_colour(t):
    """RGBW along the sunrise gradient for t in 0.0–1.0."""
    t = max(0.0, min(1.0, t))
    n = len(SUNRISE_STOPS) - 1
    scaled = t * n
    i = int(scaled)
    if i >= n:
        return SUNRISE_STOPS[-1]
    return _lerp_colour(SUNRISE_STOPS[i], SUNRISE_STOPS[i + 1], scaled - i)


# ── ColourEngine ─────────────────────────────────────────────

class ColourEngine:

    def __init__(self):
        n = NUM_GROUPS
        self._n             = n
        self._pos           = [0.0] * n
        self._target_pos    = [0.0] * n
        self._colour        = [BASE_WARM_WHITE] * n
        # Fades interpolate start → target. Lerping from the *current* colour
        # each step compounded the easing: a nominal 1 s fade was 99 % done in
        # ~0.35 s, so the Fade Speed slider barely did anything.
        self._start_col     = [BASE_WARM_WHITE] * n
        self._target_col    = [BASE_WARM_WHITE] * n
        self._fade_step     = [0] * n
        self._fading        = [False] * n
        self._w_level       = [1.0] * n
        self._w_start       = [1.0] * n
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
        # Off by default. Drift used to boot enabled, and since nothing
        # persists across reboots — and boards reboot themselves after 10 min
        # of lost MQTT — the lamps kept re-enabling it on their own no matter
        # what the web UI sent. Anyone who wants drifting can still switch it
        # on explicitly via drift_enabled over MQTT; it just has to be asked
        # for now, instead of being what the lamps do when nobody's looking.
        self._drift_enabled  = False

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
            self._start_w_fade(i, _rand_float(0.6, 1.0))

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
            # Seed appended groups from the last group's CURRENT colour —
            # seeding with warm white made those LEDs flash white for a
            # frame before fading to their target.
            last_pos = self._pos[-1]    if self._pos    else 0.0
            last_col = self._colour[-1] if self._colour else BASE_WARM_WHITE
            last_w   = self._w_level[-1] if self._w_level else 1.0
            _resize(self._pos,           last_pos)
            _resize(self._target_pos,    last_pos)
            _resize(self._colour,        last_col)
            _resize(self._start_col,     last_col)
            _resize(self._target_col,    last_col)
            _resize(self._fade_step,     0)
            _resize(self._fading,        False)
            _resize(self._w_level,       last_w)
            _resize(self._w_start,       last_w)
            _resize(self._w_target,      last_w)
            _resize(self._w_step,        0)
            _resize(self._w_fading,      False)
            # Spread the phase of any appended group. Seeding them all with
            # 0.0 made every new group breathe in perfect unison, which reads
            # as one flat pulse instead of the intended organic shimmer.
            old_bp = self._breathe_phase
            self._breathe_phase = [old_bp[k] if k < len(old_bp)
                                   else (k * 6.283185 / n) for k in range(n)]
            _resize(self._fade_steps_per, self._fade_steps)
            self._n = n
        self._group_sizes = []
        for i, g in enumerate(groups):
            self._group_sizes.append(g["size"])
            self._start_fade(i, g["pos"], fs)
            self._start_w_fade(i, g["w"])
        # Make the groups cover exactly the whole strip. A client whose sizes
        # sum to less than NUM_LEDS would otherwise leave the trailing LEDs
        # displaying stale colours from the previous scene.
        total = 0
        for k in range(len(self._group_sizes)):
            keep = max(0, min(self._group_sizes[k], NUM_LEDS - total))
            self._group_sizes[k] = keep
            total += keep
        if total < NUM_LEDS:
            self._group_sizes[-1] += NUM_LEDS - total

    def set_sunrise(self, t):
        """Put the whole strip at the sunrise colour for progress `t` (0→1).

        Immediate, with no crossfade: the alarm ramps drive colour every
        frame, so a crossfade would restart continuously and never land.
        The palette can't express this sweep — `pos` couples hue and
        saturation, so its warm end is always mostly white — hence the
        explicit RGBW keyframes in SUNRISE_STOPS.
        """
        col = sunrise_colour(t)
        for i in range(self._n):
            self._colour[i]     = col
            self._start_col[i]  = col
            self._target_col[i] = col
            self._fading[i]     = False
            # Report warm white: where the sunrise lands, and the closest
            # palette position to it for any client reading the state back
            self._pos[i]        = 0.0
            self._target_pos[i] = 0.0
            self._w_level[i]    = 1.0
            self._w_target[i]   = 1.0
            self._w_fading[i]   = False

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
            self._start_w_fade(i, new_w)

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
                self._fade_step[i] += 1
                e = _ease(self._fade_step[i] / fs)
                self._colour[i] = _lerp_colour(self._start_col[i], self._target_col[i], e)
                if self._fade_step[i] >= fs:
                    self._fading[i] = False
                    self._colour[i] = self._target_col[i]
                    self._pos[i]    = self._target_pos[i]

            # W-level fade
            if self._w_fading[i]:
                self._w_step[i] += 1
                e = _ease(self._w_step[i] / fs)
                self._w_level[i] = _lerp(self._w_start[i], self._w_target[i], e)
                if self._w_step[i] >= fs:
                    self._w_fading[i] = False
                    self._w_level[i]  = self._w_target[i]

            # Breathing — phase wraps at 2π so it never grows large enough
            # for float precision loss to make the breath jerky (it would
            # reach ~10^6 rad after a few weeks of uptime)
            ph = self._breathe_phase[i] + BREATHE_SPEED * dt
            if ph > 6.283185:
                ph -= 6.283185
            self._breathe_phase[i] = ph
            breath = 1.0 + math.sin(ph) * BREATHE_DEPTH

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
        self._start_col[group]  = self._colour[group]   # anchor for interpolation
        self._target_col[group] = _palette_colour(new_pos)
        self._fade_steps_per[group] = steps if steps is not None else self._fade_steps
        self._fade_step[group]  = 0
        self._fading[group]     = True

    def _start_w_fade(self, group, new_w):
        self._w_start[group]  = self._w_level[group]
        self._w_target[group] = new_w
        self._w_step[group]   = 0
        self._w_fading[group] = True
