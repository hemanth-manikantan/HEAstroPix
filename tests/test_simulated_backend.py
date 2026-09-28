import os
import subprocess
import sys
from pathlib import Path

from backend.simulated_hv import SimClock, SimulatedCaenDevice

REPO = Path(__file__).resolve().parent.parent


def test_ramp_physics():
    clock = SimClock()
    dev = SimulatedCaenDevice(clock)
    dev.set_ch_param(0, [0], "Pw", 1)
    dev.set_ch_param(0, [0], "V0Set", 100.0)
    clock.sleep(5)
    assert dev.get_ch_param(0, [0], "VMon") == [50.0]
    clock.sleep(20)
    assert dev.get_ch_param(0, [0], "VMon") == [100.0]


def test_pw_off_ramps_or_kills_depending_on_pdwn():
    for mode, expected in (("ramp", 80.0), ("kill", 0.0)):
        clock = SimClock()
        dev = SimulatedCaenDevice(clock, pdwn=mode, initial_state={0: (1, 100.0)})
        dev.set_ch_param(0, [0], "Pw", 0)
        clock.sleep(2)
        assert dev.get_ch_param(0, [0], "VMon") == [expected]


def test_lazy_caen_import_and_real_connect_fails_cleanly_without_it():
    code = (
        "import sys; sys.modules['caen_libs'] = None\n"
        "from backend.hvlogic import DetectorHV\n"
        "from backend.simulated_hv import SimulatedDetectorHV\n"
        "assert SimulatedDetectorHV().connect()\n"
        "assert DetectorHV('x').connect() is False\n"
        "print('ok')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("ok")


def test_cli_dry_run_never_imports_hardware_libs(tmp_path):
    csv = tmp_path / "s.csv"
    csv.write_text("0,300,1,350,2,500,60\n")
    code = ("import sys; sys.modules['caen_libs'] = None\n"
            "import run_steps\n"
            f"sys.exit(run_steps.main([{str(csv)!r}, '--channel-map', 'Grid=0,Anode=1,Cathode=2', '--dry-run']))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 0, out.stderr
    assert "Step 1: Grid=300 V" in out.stdout
    assert not (tmp_path / "run_logs").exists()   # dry-run must not create an output directory
    assert "Traceback" not in out.stderr


def test_cli_real_mode_without_caen_libs_refuses_before_connecting(tmp_path):
    csv = tmp_path / "s.csv"
    csv.write_text("0,300,1,350,2,500,60\n")
    code = ("import sys; sys.modules['caen_libs'] = None\n"
            "import run_steps\n"
            f"sys.exit(run_steps.main([{str(csv)!r}, '--channel-map', 'Grid=0,Anode=1,Cathode=2', '--yes']))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 2
    assert "caen_libs is not installed" in out.stderr and "Nothing was connected" in out.stderr
    assert not (tmp_path / "run_logs").exists()


def test_cli_rejects_bad_map_and_bad_csv(tmp_path):
    import run_steps
    good = tmp_path / "good.csv"
    good.write_text("0,300,1,350,2,500,60\n")
    bad = tmp_path / "bad.csv"
    bad.write_text("0,300,1,350,60\n")
    assert run_steps.main([str(good), "--channel-map", "Grid=0,Anode=1", "--dry-run"]) == 2   # cathode ch2 unmapped
    assert run_steps.main([str(bad), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run"]) == 2
    assert run_steps.main([str(good), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run",
                           "--daq-cmd", "daq.py {nope}"]) == 2


def test_cli_simulate_end_to_end(tmp_path, monkeypatch):
    import backend.hvlogic as hvlogic
    import run_steps
    monkeypatch.setattr(hvlogic, "time", hvlogic.time)   # restore the real clock module after install_clock
    csv = tmp_path / "s.csv"
    csv.write_text("0,100,1,150,2,200,60\n0,400,1,450,2,500,120\n")
    code = run_steps.main([str(csv), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--simulate",
                           "--outdir", str(tmp_path / "run")])
    assert code == 0
    assert (tmp_path / "run" / "run.log").exists()
    assert list((tmp_path / "run").glob("hv_log_*.csv"))


def test_cli_dry_run_prints_checkpoints_and_rejects_rule_breaking_files(tmp_path, capsys):
    import run_steps
    good = tmp_path / "good.csv"
    good.write_text("0,300,1,350,2,500,60\n")
    assert run_steps.main([str(good), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "checkpoint: Grid=0, Anode=50, Cathode=50" in out
    assert "checkpoint: Grid=300, Anode=350, Cathode=350" in out
    assert run_steps.main([str(good), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run",
                           "--stage-offset", "10"]) == 2
    for bad_row in ("0,300,1,350,2,350,60", "0,300,1,310,2,500,60", "0,300,1,350,2,340,60", "0,0,1,0,2,0,60"):
        bad = tmp_path / "bad.csv"
        bad.write_text(bad_row + "\n")
        assert run_steps.main([str(bad), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run"]) == 2, bad_row


def test_cli_dry_run_shows_daq_status_per_step(tmp_path, capsys):
    import run_steps
    csv = tmp_path / "s.csv"
    csv.write_text("0,300,1,350,2,500,60,N\n0,325,1,375,2,600,60,Y\n0,325,1,375,2,600,60\n")
    base = [str(csv), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run"]

    assert run_steps.main(base + ["--daq-cmd", "echo {step}"]) == 0
    out = capsys.readouterr().out
    lines = {l.split(":")[0].strip(): l for l in out.splitlines() if l.strip().startswith("Step")}
    assert lines["Step 1"].endswith("hold 60 s, no DAQ (CSV)")
    assert lines["Step 2"].endswith("hold 60 s, DAQ")
    assert lines["Step 3"].endswith("hold 60 s, DAQ")   # no column, follows --daq-cmd being set

    assert run_steps.main(base) == 2   # step 2 forces Y but no --daq-cmd was given: refused before any HV
    err = capsys.readouterr().err
    assert "step 2" in err and "forces a DAQ acquisition" in err


def test_cli_refuses_forced_daq_step_with_daq_backup_flags_too(tmp_path, capsys):
    import run_steps
    csv = tmp_path / "s.csv"
    csv.write_text("0,300,1,350,2,500,60,Y\n")
    code = run_steps.main([str(csv), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run"])
    assert code == 2
    assert "forces a DAQ acquisition" in capsys.readouterr().err
