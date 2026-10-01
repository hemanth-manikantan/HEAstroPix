"""Unattended step-sequence engine (no UI, no threads).

Per step: ramp (via calculate_safe_trajectory checkpoints) -> verify VMon settled
-> hold, optionally running a DAQ command while a watchdog polls ALL six crate
channels -> next step. Ends with a controlled ramp to 0 V, then power off.

With daq_continuous=True (only meaningful for the run_daq.py --daq-backup path, never for an
arbitrary --daq-cmd): an INDEFINITE ("monitor") DAQ subprocess runs by default, covering every ramp
and every non-acquiring ("N") hold, so nothing goes unmonitored. Once an acquiring ("Y") step's ramp
has settled, the monitor is stopped, a FIXED-DURATION ("science") DAQ subprocess runs for exactly
that step's hold time, and a fresh monitor subprocess starts again once it finishes, covering
everything up to the next "Y" step. tpx3-daq's Run_Datataking writes one file per run and has no way
to roll a running acquisition over to a new file, so this stop/start is what gives every "Y" step
its own distinct data file instead of it landing inside a shared monitor file. There is a brief
(tunable via --daq-startup-grace-s) gap whenever a monitor subprocess (re)starts. The final
ramp-to-zero (run end, abort, or trip) is never covered by DAQ, by design.
"""
import csv
import os
import shlex
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from backend.safety_rules import (calculate_safe_trajectory, validate_current_limits, ordering_violations,
                                  plan_ramp_checkpoints)

RAMP_RATE_V_PER_S = 10.0  # must match DetectorHV.connect() -> initialize_safety_limits(rate=10.0)
N_HW_CHANNELS = 6
SETPOINT_EPS = 0.01  # V, float noise allowance when comparing crate setpoints
EXIT_OK, EXIT_ABORTED, EXIT_REFUSED, EXIT_TRIP, EXIT_UNSAFE = 0, 1, 2, 3, 4


class RealClock:
    def time(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)


class Refused(Exception):
    """Preflight refusal; HV untouched."""


class RunAbort(Exception):
    """Operator interrupt, DAQ failure or settle timeout: controlled ramp-down."""


class SafetyTrip(Exception):
    """Watchdog violation: response chosen by on_trip."""


class CommsLost(Exception):
    pass


def build_daq_argv(template, **fields):
    try:
        return [tok.format(**fields) for tok in shlex.split(template)]
    except (KeyError, IndexError) as e:
        raise ValueError(f"unknown placeholder {e} in DAQ command; available: {sorted(fields)}") from None


def stop_process(proc, log):
    """SIGINT (lets a DAQ flush its files), then SIGTERM, then SIGKILL, on the whole process group."""
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGINT, 10), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            log.warning("DAQ process did not exit after %s", sig.name)


def checkpoints_for(cur, targets, grid_v_now, offset, min_delta):
    """Checkpoints from setpoints `cur` to `targets`: the existing 300 V staging waypoint (from
    calculate_safe_trajectory, made rule-consistent) plus the Anode/Cathode-lead ramp plan."""
    stub = SimpleNamespace(live_data={"Grid": {"V": grid_v_now}})
    waypoints = []
    for wp in calculate_safe_trajectory(stub, targets)[:-1]:
        if "Anode" in wp and "Cathode" in wp:
            wp = {**wp, "Cathode": max(wp["Cathode"], wp["Anode"])}
        waypoints.append(wp)
    return plan_ramp_checkpoints(cur, targets, offset, min_delta, waypoints)


def describe_plan(steps, channels, settle_s, offset=50.0, min_delta=20.0, daq_cmd=None, daq_continuous=False,
                  daq_startup_grace_s=0.0):
    """Human-readable plan lines (including every checkpoint) and a rough duration estimate in seconds."""
    lines = ["Channel map: " + ", ".join(f"{n}=Ch{i}" for n, i in channels.items())]
    if daq_continuous and daq_cmd:
        lines.append("DAQ: continuous monitoring by default; each 'Y' step gets its own separate, "
                     "fixed-duration acquisition for exactly its hold time, then monitoring resumes")
    total = daq_startup_grace_s if (daq_continuous and daq_cmd) else 0.0   # initial monitor segment before step 1
    prev = {n: 0.0 for n in channels}
    for k, (targets, hold, want_daq) in enumerate(steps, 1):
        acquiring = want_daq is True or (want_daq is None and daq_cmd)
        if daq_continuous and daq_cmd:
            daq_note = "science (Y)" if acquiring else "monitor only (CSV = N)" if want_daq is False else "monitor only"
            if acquiring and k < len(steps):
                total += daq_startup_grace_s   # a fresh monitor segment (re)starts after this step's science burst
        elif acquiring:
            daq_note = "DAQ"
        elif want_daq is False:
            daq_note = "no DAQ (CSV)"
        else:
            daq_note = "no DAQ"
        lines.append(f"  Step {k}: " + ", ".join(f"{n}={v:g} V" for n, v in targets.items())
                    + f", hold {hold:g} s, {daq_note}")
        cps = checkpoints_for(prev, targets, prev.get("Grid", 0.0), offset, min_delta)
        pos = prev
        for cp in cps:
            lines.append("      checkpoint: " + ", ".join(f"{n}={v:g}" for n, v in cp.items()))
            total += max(abs(cp[n] - pos[n]) for n in cp) / RAMP_RATE_V_PER_S + settle_s
            pos = cp
        total += hold
        prev = targets
    total += max(prev.values()) / RAMP_RATE_V_PER_S
    return lines, total


class StepRunner:
    def __init__(self, hv, channels, steps, outdir, *, clock=None, log, max_current_ua=5.0,
                 v_tol=10.0, hold_v_tol=50.0, settle_s=5.0, zero_tol=5.0, poll_s=1.0,
                 on_trip="shutdown", daq_cmd=None, daq_grace_s=120.0, power_on=False,
                 keep_power=False, max_read_failures=3, status_every_s=30.0,
                 min_delta=20.0, stage_offset=50.0, delta_meas_tol=5.0,
                 daq_continuous=False, daq_startup_grace_s=5.0):
        self.hv, self.channels, self.steps = hv, dict(channels), steps
        self.outdir = Path(outdir)
        self.clock = clock or RealClock()
        self.log = log
        self.max_current_ua, self.v_tol, self.hold_v_tol = max_current_ua, v_tol, hold_v_tol
        self.settle_s, self.zero_tol, self.poll_s = settle_s, zero_tol, poll_s
        self.on_trip, self.daq_cmd, self.daq_grace_s = on_trip, daq_cmd, daq_grace_s
        self.power_on, self.keep_power = power_on, keep_power
        self.max_read_failures, self.status_every_s = max_read_failures, status_every_s
        self.min_delta, self.stage_offset, self.delta_meas_tol = min_delta, stage_offset, delta_meas_tol
        self.daq_continuous, self.daq_startup_grace_s = daq_continuous, daq_startup_grace_s
        if stage_offset < min_delta:
            raise ValueError(f"stage offset {stage_offset:g} V must be >= min delta {min_delta:g} V")
        if daq_continuous and not daq_cmd:
            raise ValueError("daq_continuous requires a DAQ command")
        for k, (targets, _, want_daq) in enumerate(self.steps, 1):
            bad = ordering_violations(targets, min_delta, tol=0.0, floor=-1.0, strict_cathode=True)
            if bad:
                raise ValueError(f"step {k}: " + "; ".join(bad))
            if want_daq is True and not daq_cmd:
                raise ValueError(f"step {k}: CSV forces a DAQ acquisition (Y) but no DAQ command was configured")
        self.hv_active = False      # True once the script may have put HV on the channels
        self.expect_power = False   # mapped channels must stay powered (else: crate trip)
        self._abort_reason = None
        self._fails = 0
        self._last_status = -1e9
        self._names = {i: n for n, i in self.channels.items()}
        self.last_snap = None
        self._active_daq_proc = None  # set only while a CONTINUOUS DAQ subprocess should still be alive

    def request_abort(self, reason):
        if self._abort_reason is None:
            self._abort_reason = reason
            self.log.warning("abort requested: %s (ramp-down starts at the next poll)", reason)
        else:
            self.log.warning("already shutting down; ramp-down in progress (SIGKILL would leave HV ON)")

    # ---------------------------------------------------------------- hardware reads
    def snapshot(self):
        hv, dev = self.hv, self.hv.device
        idx = list(range(N_HW_CHANNELS))
        vset, vmon = dev.get_ch_param(hv.slot, idx, hv.PARAM_VSET), dev.get_ch_param(hv.slot, idx, hv.PARAM_VMON)
        imon, pw = dev.get_ch_param(hv.slot, idx, hv.PARAM_IMON), dev.get_ch_param(hv.slot, idx, hv.PARAM_PW)
        return {i: {"vset": float(vset[i]), "vmon": float(vmon[i]), "imon": float(imon[i]), "pw": int(pw[i])} for i in idx}

    def _poll(self):
        try:
            snap = self.snapshot()
        except Exception as e:
            self._fails += 1
            self.log.warning("HV read failed (%d/%d): %s", self._fails, self.max_read_failures, e)
            if self._fails >= self.max_read_failures:
                raise CommsLost(str(e)) from e
            return None
        self._fails = 0
        self.last_snap = snap
        for name, i in self.channels.items():
            live = self.hv.live_data.setdefault(name, {"V": 0.0, "I": 0.0, "PW": 0, "VSET": 0.0})
            live.update(V=snap[i]["vmon"], I=snap[i]["imon"], PW=snap[i]["pw"], VSET=snap[i]["vset"])
        return snap

    def _require_snapshot(self):
        while True:
            snap = self._poll()
            if snap is not None:
                return snap
            self.clock.sleep(self.poll_s)

    def _log_hv(self):
        self.hv.read_all()  # writes the CSV log; swallows its own errors (safety uses _poll instead)

    def _status(self, snap):
        if self.clock.time() - self._last_status >= self.status_every_s:
            self._last_status = self.clock.time()
            self.log.info("status: " + ", ".join(
                f"{n} {snap[i]['vmon']:.1f} V/{snap[i]['imon']:.3f} uA" for n, i in self.channels.items()))

    # ---------------------------------------------------------------- watchdog
    def _evaluate(self, snap, phase, targets=None):
        live = {self._names.get(i, f"Ch{i}"): {"I": s["imon"], "PW": s["pw"]} for i, s in snap.items()}
        if not validate_current_limits(live, self.max_current_ua):
            raise SafetyTrip(f"overcurrent (limit {self.max_current_ua} uA)")
        if self.expect_power:
            off = [n for n, i in self.channels.items() if not snap[i]["pw"]]
            if off:
                raise SafetyTrip(f"channel(s) {off} unexpectedly OFF (crate trip?)")
        bad = ordering_violations({n: snap[i]["vset"] for n, i in self.channels.items()},
                                  self.min_delta, tol=SETPOINT_EPS, floor=0.0)
        if bad:
            raise SafetyTrip("electrode rule broken by the crate's SET values: " + "; ".join(bad))
        bad = ordering_violations({n: snap[i]["vmon"] for n, i in self.channels.items()},
                                  self.min_delta, tol=self.delta_meas_tol, floor=self.zero_tol)
        if bad:
            raise SafetyTrip("electrode rule broken by MEASURED voltages: " + "; ".join(bad))
        if phase == "hold" and targets:
            drift = {n: round(snap[i]["vmon"] - targets[n], 1) for n, i in self.channels.items()
                     if abs(snap[i]["vmon"] - targets[n]) > self.hold_v_tol}
            if drift:
                raise SafetyTrip(f"VMon drifted more than {self.hold_v_tol} V from target: {drift}")

    def _sleep(self):
        self.clock.sleep(self.poll_s)
        if self._abort_reason:
            raise RunAbort(self._abort_reason)

    def _check_daq_alive(self):
        """Only set while the continuous DAQ subprocess is expected to still be running."""
        proc = self._active_daq_proc
        if proc is not None and proc.poll() is not None:
            raise RunAbort(f"continuous DAQ exited unexpectedly (code {proc.returncode})")

    def _cycle(self, phase, targets=None):
        """One watchdog cycle: wait, read, check, log. Returns the snapshot (None on a transient read failure)."""
        self._sleep()
        self._check_daq_alive()
        snap = self._poll()
        if snap is None:
            return None
        self._evaluate(snap, phase, targets)
        self._log_hv()
        self._status(snap)
        return snap

    # ---------------------------------------------------------------- phases
    def preflight(self, confirm):
        hv = self.hv
        if "Grid" not in self.channels:
            raise Refused("channel map must include 'Grid'")
        if not hv.connect():
            raise Refused("could not connect to the crate")
        hv.channels = dict(self.channels)
        hv.max_current_limit = self.max_current_ua
        stamp = time.strftime("%Y%m%d_%H%M%S")
        hv.log_filename = str(self.outdir / f"hv_log_{stamp}.csv")
        hv.reset_buffers()
        try:
            snap = self._require_snapshot()
        except CommsLost as e:
            raise Refused(f"cannot read the crate: {e}") from e
        iset = hv.device.get_ch_param(hv.slot, list(range(N_HW_CHANNELS)), hv.PARAM_ISET)
        self.log.info("crate state:")
        for i in range(N_HW_CHANNELS):
            s = snap[i]
            self.log.info("  Ch%d (%s): Pw=%d VMon=%.1f V IMon=%.3f uA I0Set=%.1f uA", i,
                          self._names.get(i, "unmapped"), s["pw"], s["vmon"], s["imon"], float(iset[i]))
        self._log_hv()

        unmapped_on = [i for i in range(N_HW_CHANNELS) if i not in self._names and snap[i]["pw"]]
        if unmapped_on:
            raise Refused(f"unmapped channel(s) {unmapped_on} are powered ON; they would be unmanaged HV")
        on = [n for n, i in self.channels.items() if snap[i]["pw"]]
        if on and len(on) != len(self.channels):
            raise Refused(f"mapped channels are partly ON ({on}); power all ON or all OFF first")
        if on:
            bad = ordering_violations({n: snap[i]["vmon"] for n, i in self.channels.items()},
                                      self.min_delta, tol=self.delta_meas_tol, floor=self.zero_tol)
            if bad:
                raise Refused("channels are already ON in a state that breaks the electrode rule: " + "; ".join(bad))
        self.needs_power_on = not on
        if self.needs_power_on and not self.power_on:
            raise Refused("mapped channels are OFF; pass --power-on to let the script power them on at 0 V")
        if not confirm():
            raise Refused("not confirmed by the operator")

    def _power_on_if_needed(self):
        self.hv_active = True
        if self.needs_power_on:
            self.log.info("powering ON mapped channels (0 V first)")
            self.hv.set_power(1, list(self.channels))
        for _ in range(15):
            snap = self._require_snapshot()
            if all(snap[i]["pw"] and (not self.needs_power_on or abs(snap[i]["vmon"]) <= self.zero_tol)
                   for i in self.channels.values()):
                self.expect_power = True
                return
            self.clock.sleep(self.poll_s)
        raise RunAbort("power-on not confirmed by the crate")

    def ramp_to(self, targets, label):
        snap = self._require_snapshot()
        jump = max(abs(v - snap[self.channels[n]]["vmon"]) for n, v in targets.items())
        timeout = 1.5 * jump / RAMP_RATE_V_PER_S + self.settle_s + 30.0
        bad = ordering_violations(targets, self.min_delta, tol=0.0, floor=0.0)
        if bad:
            raise RunAbort(f"{label}: checkpoint breaks the electrode rule: " + "; ".join(bad))
        self.log.info("%s: ramping to %s (max jump %.0f V, timeout %.0f s)", label,
                      ", ".join(f"{n}={v:g}" for n, v in targets.items()), jump, timeout)
        grid_rising = targets["Grid"] > snap[self.channels["Grid"]]["vset"]
        order = [n for n in targets if n != "Grid"]
        order = order + ["Grid"] if grid_rising else ["Grid"] + order   # leading channels first when Grid rises
        for name in order:
            self.hv.set_voltage(name, targets[name])
        t0, in_tol_since = self.clock.time(), None
        while True:
            snap = self._cycle("ramp")
            now = self.clock.time()
            if snap is not None:
                if all(abs(snap[i]["vmon"] - targets[n]) <= self.v_tol for n, i in self.channels.items()):
                    in_tol_since = in_tol_since if in_tol_since is not None else now
                    if now - in_tol_since >= self.settle_s:
                        self.log.info("%s: settled within +/-%g V", label, self.v_tol)
                        return
                else:
                    in_tol_since = None
            if now - t0 > timeout:
                raise RunAbort(f"{label}: VMon did not settle within {timeout:.0f} s")

    def go_to(self, targets, idx):
        snap = self._require_snapshot()  # also refreshes hv.live_data, which the 300 V staging decision reads
        cur = {n: snap[i]["vset"] for n, i in self.channels.items()}
        try:
            checkpoints = checkpoints_for(cur, targets, self.hv.live_data["Grid"]["V"], self.stage_offset, self.min_delta)
        except ValueError as e:
            raise RunAbort(f"step {idx + 1}: cannot plan a safe ramp: {e}") from e
        for k, checkpoint in enumerate(checkpoints, 1):
            self.ramp_to(checkpoint, f"step {idx + 1} checkpoint {k}/{len(checkpoints)}")

    def _wants_daq(self, want_daq):
        """want_daq: True (CSV forces it), False (CSV skips it), or None (follow --daq-cmd)."""
        if want_daq is False:
            return False
        if want_daq is True:
            return True  # constructor already guaranteed self.daq_cmd is set in this case
        return bool(self.daq_cmd)

    def run_all_steps(self):
        """Runs every step in order. In daq_continuous mode this alternates between an INDEFINITE monitor
        DAQ subprocess (covering ramps and non-acquiring holds) and, for each acquiring ("Y") step, a
        FIXED-DURATION science DAQ subprocess covering just that step's hold (see
        _run_steps_with_continuous_daq). Otherwise each step decides its own DAQ via hold()."""
        if self.daq_continuous:
            self._run_steps_with_continuous_daq()
            return
        for i, (targets, hold_s, want_daq) in enumerate(self.steps):
            self.log.info("=== step %d/%d ===", i + 1, len(self.steps))
            self.go_to(targets, i)
            self.hold(i, targets, hold_s, want_daq)

    def _run_steps_with_continuous_daq(self):
        """daq_continuous mode. An INDEFINITE ('--continuous') monitor DAQ subprocess runs by default,
        covering ramps and non-acquiring ("N") holds. Once an acquiring ("Y") step's ramp has settled, the
        monitor is stopped, a FIXED-DURATION science DAQ subprocess runs for exactly that step's hold time
        (its own distinct data file -- tpx3-daq has no way to roll a running acquisition over to a new
        file, so a separate science file needs a separate subprocess), and a fresh monitor subprocess is
        started again for whatever comes next. A monitor segment's data file(s) are only known once it
        stops, so N-step rows are buffered and flushed to step_data_files.csv together when that happens."""
        self._monitor_n, self._monitor_proc, self._pending_monitor_rows = 0, None, []
        self._start_monitor()
        try:
            for i, (targets, hold_s, want_daq) in enumerate(self.steps):
                self.log.info("=== step %d/%d ===", i + 1, len(self.steps))
                started = datetime.now()
                self.go_to(targets, i)
                if self._wants_daq(want_daq):
                    self._stop_monitor()
                    self._run_science_hold(i, targets, hold_s, started)
                    if i < len(self.steps) - 1:   # no point monitoring after the last step (ramp-down isn't covered)
                        self._start_monitor()
                else:
                    t0 = self.clock.time()
                    while self.clock.time() - t0 < hold_s:
                        self._cycle("hold", targets)
                    self._pending_monitor_rows.append((i + 1, started, datetime.now(), hold_s, "monitor", targets))
        finally:
            if self._monitor_proc is not None:
                self._stop_monitor()

    def _start_monitor(self):
        self._monitor_n += 1
        mdir = self.outdir / f"monitor_{self._monitor_n:02d}"
        mdir.mkdir(parents=True, exist_ok=True)
        argv = build_daq_argv(self.daq_cmd, outdir=str(mdir)) + ["--continuous"]
        self.log.info("starting monitor DAQ (segment %d): %s", self._monitor_n, shlex.join(argv))
        self._monitor_log_path = mdir / "daq.log"
        self._monitor_log_file = open(self._monitor_log_path, "w")
        self._monitor_proc = subprocess.Popen(argv, stdout=self._monitor_log_file, stderr=subprocess.STDOUT,
                                              start_new_session=True)
        self._active_daq_proc = self._monitor_proc
        t0 = self.clock.time()
        while self.clock.time() - t0 < self.daq_startup_grace_s:
            self._cycle("ramp")

    def _stop_monitor(self):
        self._active_daq_proc = None
        stop_process(self._monitor_proc, self.log)
        self._monitor_log_file.close()
        try:
            text = self._monitor_log_path.read_text(errors="replace")
        except OSError:
            text = ""
        files = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("DAQ_OUTPUT:")]
        self.log.info("monitor segment %d: data file(s): %s", self._monitor_n,
                      "; ".join(files) if files else "none reported")
        if self._pending_monitor_rows:
            self._append_step_rows(self._pending_monitor_rows, files, kind_column=True)
        self._pending_monitor_rows = []
        self._monitor_proc = None

    def _run_science_hold(self, idx, targets, hold_s, started):
        """A fixed-duration DAQ subprocess for exactly this step's hold -- run only while the indefinite
        monitor is stopped, so this is what gives a "Y" step its own distinct data file."""
        stepdir = self.outdir / f"step_{idx + 1:02d}"
        stepdir.mkdir(parents=True, exist_ok=True)
        argv = build_daq_argv(self.daq_cmd, outdir=str(stepdir)) + ["--duration", f"{hold_s:g}"]
        self.log.info("step %d: starting science DAQ (%g s): %s", idx + 1, hold_s, shlex.join(argv))
        t0, proc = self.clock.time(), None
        try:
            with open(stepdir / "daq.log", "w") as daq_log:
                # NOT tracked via _active_daq_proc/_check_daq_alive: this subprocess is SUPPOSED to exit on
                # its own once its --duration elapses, unlike the monitor, which must never exit early.
                proc = subprocess.Popen(argv, stdout=daq_log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while proc.poll() is None:
                        self._cycle("hold", targets)
                        if self.clock.time() - t0 > hold_s + self.daq_grace_s:
                            raise RunAbort(f"DAQ still running {self.daq_grace_s:g} s past the hold time")
                finally:
                    stop_process(proc, self.log)
            if proc.returncode != 0:
                raise RunAbort(f"DAQ exited with code {proc.returncode} (see {stepdir / 'daq.log'})")
            self.log.info("step %d: science DAQ finished (code 0)", idx + 1)
        finally:
            self._record_step_data(idx, targets, hold_s, started, stepdir / "daq.log", "science", kind_column=True)

    def hold(self, idx, targets, hold_s, want_daq=None):
        if self._wants_daq(want_daq):
            self._hold_with_daq(idx, targets, hold_s)
            return
        reason = "DAQ skipped for this step (CSV column = N)" if want_daq is False else "no DAQ command configured"
        self.log.info("step %d: holding %g s (%s)", idx + 1, hold_s, reason)
        t0 = self.clock.time()
        while self.clock.time() - t0 < hold_s:
            self._cycle("hold", targets)

    def _hold_with_daq(self, idx, targets, hold_s):
        stepdir = self.outdir / f"step_{idx + 1:02d}"
        stepdir.mkdir(parents=True, exist_ok=True)
        fields = {"step": idx + 1, "duration": f"{hold_s:g}", "outdir": str(stepdir),
                  **{n.lower(): f"{v:g}" for n, v in targets.items()}}
        argv = build_daq_argv(self.daq_cmd, **fields)
        self.log.info("step %d: starting DAQ: %s", idx + 1, shlex.join(argv))
        t0 = self.clock.time()
        started, proc = datetime.now(), None
        try:
            with open(stepdir / "daq.log", "w") as daq_log:
                proc = subprocess.Popen(argv, stdout=daq_log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while proc.poll() is None:
                        self._cycle("hold", targets)
                        if self.clock.time() - t0 > hold_s + self.daq_grace_s:
                            raise RunAbort(f"DAQ still running {self.daq_grace_s:g} s past the hold time")
                finally:
                    stop_process(proc, self.log)
            elapsed = self.clock.time() - t0
            if proc.returncode != 0:
                raise RunAbort(f"DAQ exited with code {proc.returncode} (see {stepdir / 'daq.log'})")
            if elapsed < 0.9 * hold_s:
                self.log.warning("step %d: DAQ finished after %.0f s, before the %g s hold", idx + 1, elapsed, hold_s)
            self.log.info("step %d: DAQ finished (code 0) after %.0f s", idx + 1, elapsed)
        finally:
            self._record_step_data(idx, targets, hold_s, started, stepdir / "daq.log",
                                   None if proc is None else proc.returncode, kind_column=False)

    def _record_step_data(self, idx, targets, hold_s, started, daq_log_path, label, kind_column):
        """Log the data file(s) the DAQ reported ('DAQ_OUTPUT: <path>') against this HV step. `label` is a
        returncode (non-continuous mode) or a "science"/"monitor" kind (daq_continuous mode); kind_column
        picks which of those the CSV column holds."""
        try:
            text = daq_log_path.read_text(errors="replace")
        except OSError:
            text = ""
        files = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("DAQ_OUTPUT:")]
        self.log.info("step %d: DAQ data file(s): %s", idx + 1, "; ".join(files) if files else "none reported")
        self._append_step_rows([(idx + 1, started, datetime.now(), hold_s, label, targets)], files,
                               kind_column=kind_column)

    def _append_step_rows(self, rows, files, kind_column):
        """rows: (step, start, end, hold_s, kind_or_returncode, targets). The wall-clock timestamps use the
        hv_log 'Timestamp' format so the files can be joined to the HV data."""
        csv_path = self.outdir / "step_data_files.csv"
        fmt = "%Y-%m-%d %H:%M:%S"
        with open(csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            if csv_path.stat().st_size == 0:
                label = "kind" if kind_column else "daq_returncode"
                writer.writerow(["step", "start", "end", "hold_s", label, "targets", "data_files"])
            for idx, start, end, hold_s, kind_or_code, targets in rows:
                writer.writerow([idx, start.strftime(fmt), end.strftime(fmt), f"{hold_s:g}", kind_or_code,
                                 ";".join(f"{n}={v:g}" for n, v in targets.items()), ";".join(files)])

    # ---------------------------------------------------------------- ramp-down
    def _wait_zero(self, timeout):
        t0 = self.clock.time()
        while True:
            snap = self._poll()
            if snap is not None:
                self._log_hv()
                self._status(snap)
                if all(abs(snap[i]["vmon"]) <= self.zero_tol for i in self.channels.values()):
                    return True
            if self.clock.time() - t0 > timeout:
                return False
            self.clock.sleep(self.poll_s)

    def _ramp_down(self, reason, use_shutdown):
        self.expect_power = False
        self.log.warning("RAMP-DOWN (%s): %s", "backend shutdown()" if use_shutdown else "controlled to 0 V", reason)
        peak = max([abs(s["vmon"]) for i, s in (self.last_snap or {}).items() if i in self._names] or [9000.0])
        timeout = 1.5 * peak / RAMP_RATE_V_PER_S + 30.0
        try:
            if use_shutdown:
                self.hv.shutdown()
            else:
                for name in self.channels:
                    self.hv.set_voltage(name, 0.0)
            reached = self._wait_zero(timeout)
            if not reached and not use_shutdown:
                self.log.critical("did not reach 0 V in %.0f s; escalating to backend shutdown()", timeout)
                self.hv.shutdown()
                reached = self._wait_zero(timeout)
            if not reached:
                return False
            if not self.keep_power:
                self.log.info("0 V verified; powering OFF mapped channels")
                self.hv.set_power(0, list(self.channels))
                self.clock.sleep(self.poll_s)
                snap = self._poll()
                if snap and any(snap[i]["pw"] for i in self.channels.values()):
                    self.log.warning("channels still report Pw=1 after power-off (voltage is at 0 V)")
            self.log.info("safe state reached")
            return True
        except CommsLost:
            raise
        except Exception as e:
            self.log.critical("ramp-down failed: %s", e)
            return False

    def _finish(self, reason, use_shutdown, code):
        if not self.hv_active:
            return code
        try:
            safe = self._ramp_down(reason, use_shutdown)
        except CommsLost as e:
            return self._comms_lost(e)
        if not safe:
            self.log.critical("COULD NOT VERIFY 0 V. CHECK THE CRATE MANUALLY NOW.")
            return EXIT_UNSAFE
        return code

    def _comms_lost(self, err):
        self.log.critical("LOST COMMUNICATION WITH THE CRATE (%s). HV MAY STILL BE ON AND UNMONITORED. "
                          "CHECK THE CRATE MANUALLY NOW.", err)
        try:
            self.hv.shutdown()
        except Exception as e:
            self.log.critical("last-resort shutdown() also failed: %s", e)
        return EXIT_UNSAFE

    # ---------------------------------------------------------------- entry point
    def run(self, confirm):
        try:
            try:
                self.preflight(confirm)
                self._power_on_if_needed()
                self.run_all_steps()
                self.log.info("all steps complete")
                return self._finish("sequence complete", False, EXIT_OK)
            except Refused as e:
                self.log.error("REFUSED: %s", e)
                return EXIT_REFUSED
            except RunAbort as e:
                self.log.warning("ABORT: %s", e)
                return self._finish(str(e), False, EXIT_ABORTED)
            except SafetyTrip as e:
                self.log.error("SAFETY TRIP: %s", e)
                return self._finish(str(e), self.on_trip == "shutdown", EXIT_TRIP)
            except CommsLost as e:
                return self._comms_lost(e)
            except Exception:
                self.log.exception("unexpected error")
                return self._finish("unexpected error", False, EXIT_ABORTED)
        finally:
            try:
                self.hv.disconnect()
            except Exception:
                pass
