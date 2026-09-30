import shlex
import sys
import threading

import pytest

from backend.simulated_hv import SimulatedDetectorHV, SimulatedCaenDevice, SimClock
from backend.step_runner import (RealClock, StepRunner, RunAbort, build_daq_argv, EXIT_OK, EXIT_ABORTED,
                                 EXIT_REFUSED, EXIT_TRIP, EXIT_UNSAFE)
from tests.helpers import MAP3, step

YES = lambda: True


def power_off_cmds(dev):
    return [c for c in dev.commands if c[1] == "Pw" and c[3] == 0]


def assert_safe_end(hv, tol=5.0):
    dev = hv.device
    assert all(abs(dev.ch[i]["vmon"]) <= tol for i in MAP3.values())
    assert all(dev.ch[i]["pw"] == 0 for i in MAP3.values())


def assert_power_cut_only_near_zero(dev, tol=5.0):
    for _, _, chans, _, vmons in power_off_cmds(dev):
        assert all(abs(vmons[c]) <= tol for c in chans if c in MAP3.values()), (chans, vmons)


# ------------------------------------------------------------------ happy path
def test_full_sequence_with_staging_and_clean_end(make_runner, caplog):
    hv, runner = make_runner([step(100, 60), step(400, 120)])
    code = runner.run(YES)
    dev = hv.device
    assert code == EXIT_OK
    assert_safe_end(hv)
    assert_power_cut_only_near_zero(dev)
    v0 = [(c[2], c[3]) for c in dev.commands if c[1] == "V0Set"]
    assert ((0,), 300.0) in v0 and ((1,), 350.0) in v0        # staging checkpoint used when crossing Grid=300
    assert ((0,), 400.0) in v0 and ((1,), 450.0) in v0
    assert dev.closed


def test_csv_log_written_with_full_rows(make_runner, tmp_path):
    hv, runner = make_runner([step(100, 30)])
    runner.run(YES)
    lines = next((tmp_path / "out").glob("hv_log_*.csv")).read_text().splitlines()
    assert len(lines) > 5
    assert "ch0_setV" in lines[0] and "ch5_status" in lines[0]
    assert all(len(l.split(",")) == len(lines[0].split(",")) for l in lines)


def test_resume_from_already_energised_channels(make_runner, vclock):
    hv = SimulatedDetectorHV(clock=vclock, initial_state={0: (1, 100.0), 1: (1, 150.0), 2: (1, 150.0)})
    _, runner = make_runner([step(100, 30)], hv=hv, power_on=False)
    assert runner.run(YES) == EXIT_OK
    assert_safe_end(hv)


# ------------------------------------------------------------------ preflight refusals (HV untouched)
def no_hv_commands(dev):
    return not [c for c in dev.commands if c[1] in ("V0Set", "Pw")]


def test_refuses_when_unmapped_channel_is_on(make_runner, vclock):
    hv = SimulatedDetectorHV(clock=vclock, initial_state={3: (1, 0.0)})
    _, runner = make_runner([step(100, 30)], hv=hv)
    assert runner.run(YES) == EXIT_REFUSED
    assert no_hv_commands(hv.device)


def test_refuses_when_channels_off_without_power_on_flag(make_runner):
    hv, runner = make_runner([step(100, 30)], power_on=False)
    assert runner.run(YES) == EXIT_REFUSED
    assert no_hv_commands(hv.device)


def test_refuses_when_partly_on(make_runner, vclock):
    hv = SimulatedDetectorHV(clock=vclock, initial_state={0: (1, 0.0)})
    _, runner = make_runner([step(100, 30)], hv=hv)
    assert runner.run(YES) == EXIT_REFUSED
    assert no_hv_commands(hv.device)


def test_refuses_when_operator_does_not_confirm(make_runner):
    hv, runner = make_runner([step(100, 30)])
    assert runner.run(lambda: False) == EXIT_REFUSED
    assert no_hv_commands(hv.device)


def test_refuses_without_grid_in_map(vclock, log, tmp_path):
    hv = SimulatedDetectorHV(clock=vclock)
    runner = StepRunner(hv, {"Anode": 1}, [({"Anode": 50.0}, 1, None)], tmp_path, clock=vclock, log=log)
    assert runner.run(YES) == EXIT_REFUSED


# ------------------------------------------------------------------ watchdog trips
def test_overcurrent_during_hold_trips_and_shuts_down(make_runner, vclock, caplog):
    hv, runner = make_runner([step(100, 600)])
    vclock.at(200, lambda: hv.device.set_current(1, 8.0))
    code = runner.run(YES)
    assert code == EXIT_TRIP
    assert_safe_end(hv)
    assert vclock.time() < 260                                  # stopped promptly, did not finish the hold


def test_overcurrent_on_unmapped_channel_is_also_caught(make_runner, vclock):
    hv, runner = make_runner([step(100, 600)])
    vclock.at(200, lambda: (hv.device.ch[4].update(pw=1), hv.device.set_current(4, 9.0)))
    assert runner.run(YES) == EXIT_TRIP
    # unmapped channel 4 was switched on by the "world", not the script: it is not ours to power off,
    # but the run must still stop and ramp OUR channels down
    assert all(abs(hv.device.ch[i]["vmon"]) <= 5 for i in MAP3.values())


def test_overcurrent_during_ramp(make_runner, vclock):
    hv, runner = make_runner([step(400, 60)])
    vclock.at(20, lambda: hv.device.set_current(0, 6.0))
    assert runner.run(YES) == EXIT_TRIP
    assert_safe_end(hv)


def test_on_trip_ramp_option_uses_controlled_rampdown(make_runner, vclock):
    hv, runner = make_runner([step(100, 600)], on_trip="ramp")
    vclock.at(200, lambda: hv.device.set_current(1, 8.0))
    assert runner.run(YES) == EXIT_TRIP
    assert_safe_end(hv)
    assert_power_cut_only_near_zero(hv.device)


def test_crate_side_trip_channel_off_is_detected(make_runner, vclock):
    # hold_v_tol is huge so ONLY the "unexpectedly OFF" check can catch this
    hv, runner = make_runner([step(100, 600)], hold_v_tol=1e9)
    vclock.at(200, lambda: hv.device.ch[1].update(pw=0))
    assert runner.run(YES) == EXIT_TRIP
    assert vclock.time() < 260
    assert all(abs(hv.device.ch[i]["vmon"]) <= 5 for i in MAP3.values())


def test_voltage_drift_during_hold_trips(make_runner, vclock):
    # setpoint silently changed on the crate: Pw stays 1, currents are fine, only the drift check sees it
    hv, runner = make_runner([step(100, 600)])
    vclock.at(200, lambda: hv.device.ch[0].update(v0set=10.0))
    assert runner.run(YES) == EXIT_TRIP
    assert vclock.time() < 300
    assert_safe_end(hv)


# ------------------------------------------------------------------ aborts
def test_operator_abort_ramps_down_before_cutting_power(make_runner, vclock):
    hv, runner = make_runner([step(300, 600)])
    vclock.at(200, lambda: runner.request_abort("test SIGINT"))
    code = runner.run(YES)
    assert code == EXIT_ABORTED
    assert_safe_end(hv)
    assert_power_cut_only_near_zero(hv.device)
    assert vclock.time() < 300


def test_settle_timeout_aborts_safely(make_runner, vclock):
    hv, runner = make_runner([step(100, 60)])
    vclock.at(0.5, lambda: hv.device.frozen.add(0))               # Grid never leaves 0 V
    code = runner.run(YES)
    assert code == EXIT_ABORTED
    assert_safe_end(hv)


def test_kill_mode_crate_still_ends_safe(make_runner, vclock):
    hv = SimulatedDetectorHV(clock=vclock, pdwn="kill")
    _, runner = make_runner([step(100, 30)], hv=hv)
    assert runner.run(YES) == EXIT_OK
    assert_safe_end(hv)


# ------------------------------------------------------------------ comms loss / cannot verify
def test_comms_loss_reports_unsafe_and_does_not_raise(make_runner, vclock, caplog):
    hv, runner = make_runner([step(100, 600)])
    vclock.at(200, lambda: setattr(hv.device, "comms_down", True))
    with caplog.at_level("INFO", logger="hv_steps_test"):
        code = runner.run(YES)
    assert code == EXIT_UNSAFE
    assert "LOST COMMUNICATION" in caplog.text


def test_transient_read_failures_are_tolerated(make_runner, vclock):
    hv, runner = make_runner([step(100, 60)])
    vclock.at(100, lambda: setattr(hv.device, "comms_down", True))
    vclock.at(101.5, lambda: setattr(hv.device, "comms_down", False))
    assert runner.run(YES) == EXIT_OK


def test_unreachable_zero_reports_unsafe(make_runner, vclock):
    hv, runner = make_runner([step(100, 600)])
    vclock.at(200, lambda: (hv.device.frozen.add(0), runner.request_abort("test")))   # Grid stuck at 100 V
    assert runner.run(YES) == EXIT_UNSAFE


# ------------------------------------------------------------------ DAQ subprocess (real clock, fast sim crate)
@pytest.fixture
def real_runner(log, tmp_path):
    (tmp_path / "out").mkdir(exist_ok=True)

    def factory(daq_code, hold=1.0, steps=None, **kw):
        hv = SimulatedDetectorHV(clock=RealClock(), ramp_rate=1e6)
        cmd = shlex.join([sys.executable, "-c", daq_code, "{outdir}/args.txt", "{step}", "{duration}", "{grid}"])
        kw.setdefault("daq_grace_s", 1.0)
        runner = StepRunner(hv, MAP3, steps or [step(100, hold)], tmp_path / "out", clock=RealClock(), log=log,
                            settle_s=0.1, poll_s=0.05, daq_cmd=cmd, power_on=True, **kw)
        return hv, runner
    return factory


WRITE_ARGS = "import sys; open(sys.argv[1],'w').write(' '.join(sys.argv[2:]))"


def test_daq_receives_placeholders_and_success_continues(real_runner, tmp_path):
    hv, runner = real_runner(WRITE_ARGS)
    assert runner.run(YES) == EXIT_OK
    assert (tmp_path / "out/step_01/args.txt").read_text() == "1 1 100"
    assert_safe_end(hv)


def test_daq_failure_aborts_and_ramps_down(real_runner):
    hv, runner = real_runner("import sys; sys.exit(3)")
    assert runner.run(YES) == EXIT_ABORTED
    assert_safe_end(hv)


def test_daq_overrun_is_killed(real_runner):
    hv, runner = real_runner("import time; time.sleep(60)", hold=0.3, daq_grace_s=0.3)
    assert runner.run(YES) == EXIT_ABORTED
    assert_safe_end(hv)


def test_overcurrent_kills_running_daq(real_runner):
    hv, runner = real_runner("import time; time.sleep(60)", hold=30, daq_grace_s=30)
    threading.Timer(1.0, lambda: hv.device.set_current(1, 9.0)).start()
    assert runner.run(YES) == EXIT_TRIP
    assert_safe_end(hv)


def test_build_daq_argv():
    assert build_daq_argv("daq.py --t {duration} --s {step}", duration="5", step=2) == ["daq.py", "--t", "5", "--s", "2"]
    with pytest.raises(ValueError, match="unknown placeholder"):
        build_daq_argv("daq.py {nope}", step=1)


# ------------------------------------------------------------------ electrode ordering rule
from backend.safety_rules import ordering_violations


def record_vmon(monkeypatch):
    seen = []
    orig = SimulatedCaenDevice.get_ch_param

    def spy(self, slot, channels, param):
        out = orig(self, slot, channels, param)
        if param == "VMon":
            seen.append({"Grid": out[0], "Anode": out[1], "Cathode": out[2]})
        return out
    monkeypatch.setattr(SimulatedCaenDevice, "get_ch_param", spy)
    return seen


def test_rule_holds_on_every_measurement_of_a_whole_run(make_runner, monkeypatch):
    seen = record_vmon(monkeypatch)
    hv, runner = make_runner([step(300, 60), step(150, 60), step(500, 60)])
    assert runner.run(YES) == EXIT_OK
    assert len(seen) > 100
    assert [s for s in seen if ordering_violations(s, 20.0, tol=0.5, floor=0.0)] == []
    assert max(s["Grid"] for s in seen) >= 500


def test_anode_and_cathode_lead_the_grid_when_ramping_up(make_runner):
    hv, runner = make_runner([step(300, 30)])
    assert runner.run(YES) == EXIT_OK
    nonzero = [(c[2][0], c[3]) for c in hv.device.commands if c[1] == "V0Set" and c[3] > 0]
    assert nonzero[:2] == [(1, 50.0), (2, 50.0)] or nonzero[:2] == [(2, 50.0), (1, 50.0)]   # lead first, Grid still 0
    assert nonzero.index((1, 350.0)) < nonzero.index((0, 300.0))                          # leaders set before Grid rises
    assert nonzero.index((0, 300.0)) < nonzero.index((2, 400.0))                          # final Cathode ramp after Grid arrived


def test_measured_voltage_breaking_the_rule_trips_even_when_setpoints_look_fine(make_runner, vclock, caplog):
    hv, runner = make_runner([step(100, 600)], hold_v_tol=1e9)     # drift check off: only the ordering check can see this
    vclock.at(200, lambda: hv.device.ch[1].update(vmon=hv.device.ch[0]["vmon"] - 20))
    with caplog.at_level("INFO", logger="hv_steps_test"):
        assert runner.run(YES) == EXIT_TRIP
    assert "MEASURED" in caplog.text
    assert vclock.time() < 260


def test_setpoint_change_on_the_crate_breaking_the_rule_trips(make_runner, vclock, caplog):
    hv, runner = make_runner([step(100, 600)], hold_v_tol=1e9)
    vclock.at(200, lambda: hv.device.ch[1].update(v0set=110.0))    # Anode only 10 V above Grid
    with caplog.at_level("INFO", logger="hv_steps_test"):
        assert runner.run(YES) == EXIT_TRIP
    assert "SET values" in caplog.text


def test_refuses_to_start_from_channels_already_on_in_a_rule_breaking_state(make_runner, vclock):
    hv = SimulatedDetectorHV(clock=vclock, initial_state={0: (1, 325.0), 1: (1, 325.0), 2: (1, 375.0)})
    _, runner = make_runner([step(400, 30)], hv=hv, power_on=False)
    assert runner.run(YES) == EXIT_REFUSED
    assert no_hv_commands(hv.device)


@pytest.mark.parametrize("bad_step", [
    ({"Grid": 300.0, "Anode": 350.0, "Cathode": 350.0}, 10, None),   # cathode not above anode
    ({"Grid": 300.0, "Anode": 315.0, "Cathode": 500.0}, 10, None),   # anode < grid + 20
    ({"Grid": 0.0, "Anode": 0.0, "Cathode": 0.0}, 10, None),
])
def test_runner_rejects_rule_breaking_steps_at_construction(vclock, log, tmp_path, bad_step):
    with pytest.raises(ValueError, match="step 1"):
        StepRunner(SimulatedDetectorHV(clock=vclock), MAP3, [bad_step], tmp_path, clock=vclock, log=log)


def test_runner_rejects_offset_below_min_delta(vclock, log, tmp_path):
    with pytest.raises(ValueError, match="offset"):
        StepRunner(SimulatedDetectorHV(clock=vclock), MAP3, [step(100, 10)], tmp_path, clock=vclock, log=log,
                   stage_offset=10.0)


# ------------------------------------------------------------------ DAQ data files logged against the HV step
def read_step_csv(tmp_path):
    import csv
    return list(csv.DictReader(open(tmp_path / "out" / "step_data_files.csv")))


def test_data_files_reported_by_the_daq_are_logged_against_the_step(real_runner, tmp_path):
    hv, runner = real_runner("print('DAQ_OUTPUT: /x/a.h5'); print('DAQ_OUTPUT: /x/b.h5'); print('noise')")
    assert runner.run(YES) == EXIT_OK
    (row,) = read_step_csv(tmp_path)
    assert row["step"] == "1" and row["daq_returncode"] == "0" and row["hold_s"] == "1"
    assert row["data_files"] == "/x/a.h5;/x/b.h5"
    assert "Grid=100" in row["targets"] and "Cathode=200" in row["targets"]
    assert row["start"] <= row["end"] and len(row["start"]) == 19          # hv_log Timestamp format


def test_step_record_is_written_even_when_the_daq_fails(real_runner, tmp_path):
    hv, runner = real_runner("print('DAQ_OUTPUT: /x/partial.h5'); import sys; sys.exit(3)")
    assert runner.run(YES) == EXIT_ABORTED
    (row,) = read_step_csv(tmp_path)
    assert row["daq_returncode"] == "3" and row["data_files"] == "/x/partial.h5"


def test_step_record_is_written_when_a_trip_stops_the_daq(real_runner, tmp_path):
    hv, runner = real_runner("import time; time.sleep(60)", hold=30, daq_grace_s=30)
    threading.Timer(1.0, lambda: hv.device.set_current(1, 9.0)).start()
    assert runner.run(YES) == EXIT_TRIP
    (row,) = read_step_csv(tmp_path)
    assert row["daq_returncode"] not in ("", "0") and row["data_files"] == ""


# ------------------------------------------------------------------ per-step DAQ Y/N column resolution
def test_daq_flag_per_step_controls_whether_daq_runs(real_runner, tmp_path):
    steps = [step(100, 1, daq=False), step(200, 1, daq=True), step(300, 1, daq=None)]
    hv, runner = real_runner(WRITE_ARGS, steps=steps)
    assert runner.run(YES) == EXIT_OK
    assert not (tmp_path / "out/step_01/args.txt").exists()   # CSV said N: no DAQ even though --daq-cmd is set
    assert (tmp_path / "out/step_02/args.txt").exists()       # CSV said Y: DAQ runs
    assert (tmp_path / "out/step_03/args.txt").exists()       # no column: follows --daq-cmd being set -> DAQ runs
    rows = read_step_csv(tmp_path)
    assert [r["step"] for r in rows] == ["2", "3"]             # only DAQ-run steps get a data-file record


def test_daq_column_none_without_a_daq_cmd_just_holds(make_runner):
    # no daq_cmd configured at all: a column-less step (daq=None) must behave exactly as before this feature
    hv, runner = make_runner([step(100, 30, daq=None)])
    assert runner.run(YES) == EXIT_OK
    assert no_hv_commands  # sanity the helper still exists; real assertion is that run() completed via plain hold


def test_daq_column_false_skips_even_with_a_daq_cmd_configured_via_simulated_clock(make_runner):
    hv, runner = make_runner([step(100, 5, daq=False)], daq_cmd="/bin/false {step}")
    assert runner.run(YES) == EXIT_OK   # would fail/hang trying to run /bin/false as a real subprocess if not skipped


def test_constructor_rejects_a_forced_y_step_without_a_daq_cmd(vclock, log, tmp_path):
    with pytest.raises(ValueError, match="forces a DAQ"):
        StepRunner(SimulatedDetectorHV(clock=vclock), MAP3, [step(100, 10, daq=True)], tmp_path,
                  clock=vclock, log=log)


def test_wants_daq_resolution_table():
    hv, runner = None, StepRunner.__new__(StepRunner)
    for daq_cmd in (None, "cmd"):
        runner.daq_cmd = daq_cmd
        assert runner._wants_daq(False) is False
        assert runner._wants_daq(None) is bool(daq_cmd)
    runner.daq_cmd = "cmd"
    assert runner._wants_daq(True) is True


# ------------------------------------------------------------------ continuous DAQ (covers ramp + hold)
def test_check_daq_alive_noop_when_unset_or_alive(make_runner):
    hv, runner = make_runner([step(100, 30)])
    runner._check_daq_alive()  # no proc tracked: no-op

    class AliveProc:
        def poll(self):
            return None
    runner._active_daq_proc = AliveProc()
    runner._check_daq_alive()  # still alive: no-op


def test_check_daq_alive_raises_when_the_tracked_process_has_exited(make_runner):
    hv, runner = make_runner([step(100, 30)])

    class DeadProc:
        returncode = 5
        def poll(self):
            return 5
    runner._active_daq_proc = DeadProc()
    with pytest.raises(RunAbort, match="exited unexpectedly"):
        runner._check_daq_alive()


def test_constructor_rejects_daq_continuous_without_a_daq_cmd(vclock, log, tmp_path):
    with pytest.raises(ValueError, match="daq_continuous requires"):
        StepRunner(SimulatedDetectorHV(clock=vclock), MAP3, [step(100, 10, daq=None)], tmp_path,
                  clock=vclock, log=log, daq_continuous=True)


# Stands in for run_daq.py in both of its modes: with "--continuous" (as the monitor is launched) it runs
# until signalled; with "--duration N" (as a science burst is launched) it self-exits after N seconds. It
# tells the two apart the same way run_daq.py would: by which flags follow its --outdir argument.
HYBRID_MARKER = (
    "import sys, signal, time\n"
    "outdir = sys.argv[1]\n"
    "rest = sys.argv[2:]\n"
    "duration = float(rest[rest.index('--duration') + 1]) if '--duration' in rest else None\n"
    "print('DAQ_OUTPUT: ' + outdir + '/data.h5', flush=True)\n"
    "open(outdir + '/started.txt', 'w').write(str(time.time()))\n"
    "stop = []\n"
    "signal.signal(signal.SIGINT, lambda *a: stop.append(1))\n"
    "signal.signal(signal.SIGTERM, lambda *a: stop.append(1))\n"
    "hb = open(outdir + '/heartbeat.txt', 'a')\n"
    "t0 = time.time()\n"
    "while not stop and (duration is None or time.time() - t0 < duration):\n"
    "    hb.write(str(time.time()) + chr(10)); hb.flush()\n"
    "    time.sleep(0.02)\n"
    "open(outdir + '/stopped.txt', 'w').write(str(time.time()))\n"
    "sys.exit(0)\n"
)


@pytest.fixture
def continuous_runner(log, tmp_path):
    (tmp_path / "out").mkdir(exist_ok=True)

    def factory(daq_code, hold=0.5, steps=None, ramp_rate=50.0, **kw):
        hv = SimulatedDetectorHV(clock=RealClock(), ramp_rate=ramp_rate)
        cmd = shlex.join([sys.executable, "-c", daq_code, "{outdir}"])
        kw.setdefault("daq_startup_grace_s", 0.3)
        kw.setdefault("daq_grace_s", 1.0)
        runner = StepRunner(hv, MAP3, steps or [step(100, hold)], tmp_path / "out", clock=RealClock(), log=log,
                            settle_s=0.1, poll_s=0.05, daq_cmd=cmd, power_on=True, daq_continuous=True, **kw)
        return hv, runner
    return factory


def test_continuous_daq_starts_before_the_first_ramp_and_stays_alive_through_it(continuous_runner, tmp_path):
    # a lone step with no CSV column follows --daq-cmd being set, i.e. it's an acquiring ("Y") step: its ramp
    # is covered by monitor segment 1, which must be alive before any voltage moves
    hv, runner = continuous_runner(HYBRID_MARKER)
    assert runner.run(YES) == EXIT_OK
    mdir = tmp_path / "out" / "monitor_01"
    started = float((mdir / "started.txt").read_text())
    heartbeats = [float(l) for l in (mdir / "heartbeat.txt").read_text().splitlines() if l.strip()]

    v0set_times = [c[0] for c in hv.device.commands if c[1] == "V0Set" and c[3] != 0.0]
    assert v0set_times, "the step must have actually ramped"
    first_nonzero_vset = min(v0set_times)

    assert started < first_nonzero_vset, "the monitor DAQ must be launched before any voltage is commanded"
    assert any(h > first_nonzero_vset for h in heartbeats), "monitor DAQ must still be alive during/after the ramp"
    assert_safe_end(hv)


def test_continuous_daq_covers_every_step_including_n_steps_ramps(continuous_runner, tmp_path):
    # the whole point of the feature: an N step's RAMP must still be monitored, not skipped entirely, AND a
    # "Y" step must get its OWN distinct file rather than sharing the ongoing monitor's file
    steps_list = [step(100, 0.3, daq=False), step(300, 0.3, daq=True), step(150, 0.3, daq=False)]
    hv, runner = continuous_runner(HYBRID_MARKER, steps=steps_list)
    assert runner.run(YES) == EXIT_OK
    outdir = tmp_path / "out"
    v0set = sorted((c[0], c[2][0]) for c in hv.device.commands if c[1] == "V0Set" and c[3] != 0.0)

    def started_stopped(name):
        d = outdir / name
        return float((d / "started.txt").read_text()), float((d / "stopped.txt").read_text())

    # monitor segment 1 covers step 1's ramp+hold and step 2's ramp-in; monitor segment 2 (started once
    # step 2's science burst finishes) covers step 3's ramp+hold
    m1_start, m1_stop = started_stopped("monitor_01")
    sci_start, sci_stop = started_stopped("step_02")
    m2_start, m2_stop = started_stopped("monitor_02")
    assert m1_stop <= sci_start, "the monitor must stop before step 2's science burst starts"
    assert sci_stop <= m2_start, "step 2's science burst must finish before the next monitor segment starts"
    assert any(m1_start < t < m1_stop for t, _ in v0set), "step 1's ramp must be covered by monitor segment 1"
    assert any(m2_start < t < m2_stop for t, _ in v0set), "step 3's ramp must be covered by monitor segment 2"

    assert_safe_end(hv)

    import csv as csvmod
    rows = list(csvmod.DictReader(open(outdir / "step_data_files.csv")))
    assert [r["step"] for r in rows] == ["1", "2", "3"]
    assert [r["kind"] for r in rows] == ["monitor", "science", "monitor"]
    assert len(set(r["data_files"] for r in rows)) == 3, "the science step and both monitor segments must all differ"
    assert rows[0]["data_files"] == str(outdir / "monitor_01" / "data.h5")
    assert rows[1]["data_files"] == str(outdir / "step_02" / "data.h5")
    assert rows[2]["data_files"] == str(outdir / "monitor_02" / "data.h5")


def test_continuous_daq_back_to_back_y_steps_each_get_their_own_file(continuous_runner, tmp_path):
    # the exact scenario originally reported: consecutive acquiring steps must not merge into one file
    steps_list = [step(100, 0.3, daq=True), step(300, 0.3, daq=True)]
    hv, runner = continuous_runner(HYBRID_MARKER, steps=steps_list)
    assert runner.run(YES) == EXIT_OK
    outdir = tmp_path / "out"
    assert (outdir / "monitor_02").exists(), "the ramp between the two science bursts must still be monitored"

    import csv as csvmod
    rows = list(csvmod.DictReader(open(outdir / "step_data_files.csv")))
    assert [r["step"] for r in rows] == ["1", "2"]
    assert [r["kind"] for r in rows] == ["science", "science"]
    assert rows[0]["data_files"] == str(outdir / "step_01" / "data.h5")
    assert rows[1]["data_files"] == str(outdir / "step_02" / "data.h5")
    assert rows[0]["data_files"] != rows[1]["data_files"]


def test_continuous_daq_dying_during_startup_grace_blocks_the_ramp(continuous_runner):
    hv, runner = continuous_runner("import sys; sys.exit(9)")
    assert runner.run(YES) == EXIT_ABORTED
    # power-on-at-0V is a required precondition and does happen; the ramp toward the step's target must not
    assert all(c[3] == 0.0 for c in hv.device.commands if c[1] == "V0Set")
    assert_safe_end(hv)


def test_continuous_daq_dying_mid_sequence_aborts_and_ramps_down(continuous_runner):
    # the monitor (segment 1, covering step 1) behaves normally; step 2's dedicated SCIENCE burst dies
    # partway into its long hold -- distinguished by "step_" appearing in its own --outdir argument
    script = (
        "import sys, signal, time\n"
        "outdir = sys.argv[1]\n"
        "if 'step_' in outdir:\n"
        "    time.sleep(0.2); sys.exit(1)\n"
        "else:\n"
        "    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\n"
        "    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
        "    while True: time.sleep(0.05)\n"
    )
    steps_list = [step(100, 0.3, daq=False), step(300, 3.0, daq=True)]  # step 2's science DAQ dies mid-hold
    hv, runner = continuous_runner(script, steps=steps_list)
    assert runner.run(YES) == EXIT_ABORTED
    assert_safe_end(hv)


def test_describe_plan_notes_continuous_mode():
    from backend.step_runner import describe_plan
    lines, _ = describe_plan([step(300, 60, daq=True), step(400, 30, daq=False)], MAP3, settle_s=5.0,
                             daq_cmd="x", daq_continuous=True)
    assert any("continuous monitoring by default" in l for l in lines)
    assert any("science (Y)" in l for l in lines)
    assert any("monitor only (CSV = N)" in l for l in lines)
