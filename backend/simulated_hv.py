"""Offline stand-in for the CAEN crate. Never imports caen_libs.

SimulatedDetectorHV subclasses DetectorHV and only replaces connect(), so all
real logic (read_all, set_power, shutdown, logging, ...) runs unchanged on top
of a fake device that models ramping, power-down and a crate-side current trip.
"""
from backend.hvlogic import DetectorHV


class SimClock:
    """Virtual time: sleep() advances instantly. Use install_clock() so hvlogic uses it too."""

    def __init__(self, start=0.0):
        self._t = float(start)
        self._events = []  # (time, fn), fired once when virtual time passes them

    def time(self):
        return self._t

    def sleep(self, seconds):
        self._t += max(0.0, float(seconds))
        due = [e for e in self._events if e[0] <= self._t]
        self._events = [e for e in self._events if e[0] > self._t]
        for _, fn in sorted(due, key=lambda e: e[0]):
            fn()

    def at(self, t, fn):
        self._events.append((float(t), fn))


def install_clock(clock):
    """Make backend.hvlogic use `clock` for time.time()/time.sleep(). Returns the old module."""
    import backend.hvlogic as hvlogic
    old = hvlogic.time
    hvlogic.time = clock
    return old


_PARAMS = {"V0Set": "v0set", "VMon": "vmon", "I0Set": "i0set", "IMon": "imon",
           "Pw": "pw", "RUp": "rup", "RDWn": "rdwn"}


class SimulatedCaenDevice:
    def __init__(self, clock, n_channels=6, ramp_rate=10.0, pdwn="ramp", i0set=100.0, initial_state=None):
        """pdwn: 'ramp' (Pw=0 ramps down at RDWn) or 'kill' (Pw=0 drops output immediately).
        initial_state: {channel: (pw, volts)} for channels that start powered/energised."""
        self.clock = clock
        self.pdwn = pdwn
        self.ch = [dict(v0set=0.0, vmon=0.0, i0set=i0set, imon=0.0, pw=0, rup=ramp_rate, rdwn=ramp_rate)
                   for _ in range(n_channels)]
        for idx, (pw, volts) in (initial_state or {}).items():
            self.ch[idx].update(pw=int(pw), v0set=float(volts), vmon=float(volts))
        self.injected_i = {}
        self.frozen = set()
        self.comms_down = False
        self.commands = []  # (time, param, channels, value, vmon of all channels at that moment)
        self.closed = False
        self._last = clock.time()

    def set_current(self, channel, microamps):
        self.injected_i[channel] = microamps

    def _guard(self):
        if self.comms_down:
            raise ConnectionError("simulated communication loss")

    def _advance(self):
        now = self.clock.time()
        dt, self._last = now - self._last, now
        if dt <= 0:
            return
        for i, c in enumerate(self.ch):
            if i in self.frozen:
                continue
            if not c["pw"] and self.pdwn == "kill":
                c["vmon"] = 0.0
            else:
                target = c["v0set"] if c["pw"] else 0.0
                rate = c["rup"] if target > c["vmon"] else c["rdwn"]
                delta = target - c["vmon"]
                c["vmon"] += max(-rate * dt, min(rate * dt, delta))
            c["imon"] = self.injected_i.get(i, 0.0) if c["pw"] else 0.0
            if c["pw"] and c["imon"] > c["i0set"]:
                c["pw"] = 0  # crate-side hardware trip

    def get_ch_param(self, slot, channels, param):
        self._guard()
        self._advance()
        key = _PARAMS[param]
        return [self.ch[c][key] for c in channels]

    def set_ch_param(self, slot, channels, param, value):
        self._guard()
        self._advance()
        key = _PARAMS[param]
        self.commands.append((self.clock.time(), param, tuple(channels), value, [c["vmon"] for c in self.ch]))
        for c in channels:
            self.ch[c][key] = value if key != "pw" else int(value)

    def close(self):
        self.closed = True


class SimulatedDetectorHV(DetectorHV):
    def __init__(self, address="simulated", user="admin", password="admin", clock=None, **device_kwargs):
        super().__init__(address, user, password)
        self.sim_clock = clock or SimClock()
        self._device_kwargs = device_kwargs

    def connect(self):
        self.device = SimulatedCaenDevice(self.sim_clock, **self._device_kwargs)
        self.initialize_safety_limits(rate=10.0)  # same call the real connect() makes
        if "ramp_rate" in self._device_kwargs:  # test-only: let the sim ramp faster than the real 10 V/s
            for c in self.device.ch:
                c.update(rup=self._device_kwargs["ramp_rate"], rdwn=self._device_kwargs["ramp_rate"])
        return True
