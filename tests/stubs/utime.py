_now = [0]
def ticks_ms(): return _now[0]
def ticks_diff(a, b): return a - b
def ticks_add(a, b): return a + b
def time(): return _now[0] // 1000
def sleep_ms(ms): _now[0] += ms
def sleep_us(us): _now[0] += max(1, us // 1000)
def sleep(s): _now[0] += int(s * 1000)
import time as _t, calendar as _c
def localtime(secs=None):
    if secs is None:
        s = _now[0] // 1000
        return (2026, 7, 27, (s // 3600) % 24, (s // 60) % 60, s % 60, 0, 208)
    g = _t.gmtime(secs)
    return (g.tm_year, g.tm_mon, g.tm_mday, g.tm_hour, g.tm_min, g.tm_sec, g.tm_wday, g.tm_yday)
def mktime(t):
    return int(_c.timegm((t[0], t[1], t[2], t[3], t[4], t[5], 0, 0, 0)))
def advance(ms): _now[0] += ms
