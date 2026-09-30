import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import run_daq
import run_steps

REPO = Path(__file__).resolve().parent.parent


class FakeBackend:
    def __init__(self, data_dir, produce=True, set_ok=True, write_ok=True, interrupt=False, size=5):
        self.data_dir, self.produce, self.set_ok, self.write_ok = data_dir, produce, set_ok, write_ok
        self.interrupt, self.size = interrupt, size
        self.calls, self.values = [], {}

    def set_data(self, data):
        self.calls.append(("set_data", data))
        return self.set_ok

    def write_backup_to_yaml(self):
        self.calls.append(("yaml",))

    def write_value(self, name, value):
        self.calls.append(("write_value", name, value))
        if self.write_ok:
            self.values[name] = value
        return self.write_ok

    def read_value(self, name):
        return self.values.get(name)

    def run_datataking(self, seconds):
        self.calls.append(("run", seconds))
        if self.produce:
            (self.data_dir / "DataTake_2026-01-01_00-00-00.h5").write_bytes(b"x" * self.size)
        if self.interrupt:
            raise KeyboardInterrupt


@pytest.fixture
def files(tmp_path):
    (tmp_path / "hdf").mkdir()
    backup, mask, eq = tmp_path / "b.TPX3", tmp_path / "m.h5", tmp_path / "e.h5"
    backup.write_text(json.dumps({"Ibias_Ikrum": 7}))
    mask.write_bytes(b"mask")
    eq.write_bytes(b"eq")
    return backup, mask, eq


def run(files, tmp_path, backend, duration="600", extra=()):
    backup, mask, eq = files
    return run_daq.main(["--backup", str(backup), "--mask", str(mask), "--equalisation", str(eq),
                         "--duration", duration, "--data-dir", str(tmp_path / "hdf"), *extra], backend=backend)


def run_raw(files, tmp_path, backend, extra=()):
    backup, mask, eq = files
    return run_daq.main(["--backup", str(backup), "--mask", str(mask), "--equalisation", str(eq),
                         "--data-dir", str(tmp_path / "hdf"), *extra], backend=backend)


def test_happy_path_loads_everything_by_full_path_then_runs_and_reports_the_file(files, tmp_path, capsys):
    backend = FakeBackend(tmp_path / "hdf")
    assert run(files, tmp_path, backend, duration="600.4", extra=["--step", "3"]) == 0
    backup, mask, eq = files
    assert backend.calls == [("set_data", {"Ibias_Ikrum": 7}), ("yaml",),
                             ("write_value", "Mask_path", str(mask.resolve())),
                             ("write_value", "Equalisation_path", str(eq.resolve())),
                             ("run", 600)]                                  # settings loaded strictly before the run
    out = capsys.readouterr().out
    assert f"DAQ_OUTPUT: {tmp_path / 'hdf' / 'DataTake_2026-01-01_00-00-00.h5'}" in out
    assert "step=3" in out and "duration_s=600" in out


@pytest.mark.parametrize("duration", ["0", "0.4", "-5", "nan"])
def test_zero_or_negative_duration_is_refused_because_zero_means_infinite(files, tmp_path, duration):
    backend = FakeBackend(tmp_path / "hdf")
    assert run(files, tmp_path, backend, duration=duration) == 1
    assert backend.calls == []


def test_missing_or_empty_or_invalid_files_stop_before_touching_tpx3(files, tmp_path, capsys):
    backup, mask, eq = files
    for bad, fragment in ((backup, "not valid JSON"), (mask, "empty"), (eq, "not found")):
        backend = FakeBackend(tmp_path / "hdf")
        saved = bad.read_bytes()
        if bad is backup:
            bad.write_text("{not json")
        elif bad is mask:
            bad.write_bytes(b"")
        else:
            bad.rename(tmp_path / "moved.h5")
        assert run(files, tmp_path, backend) == 1
        assert backend.calls == []
        assert fragment in capsys.readouterr().err
        if bad is eq:
            (tmp_path / "moved.h5").rename(bad)
        else:
            bad.write_bytes(saved)


def test_backup_rejected_by_tpx3_stops_before_the_run(files, tmp_path):
    backend = FakeBackend(tmp_path / "hdf", set_ok=False)
    assert run(files, tmp_path, backend) == 1
    assert not [c for c in backend.calls if c[0] == "run"]


def test_a_path_that_did_not_stick_stops_before_the_run(files, tmp_path):
    backend = FakeBackend(tmp_path / "hdf", write_ok=False)
    assert run(files, tmp_path, backend) == 1
    assert not [c for c in backend.calls if c[0] == "run"]


def test_no_new_data_file_is_a_failure_even_if_old_files_exist(files, tmp_path):
    old = tmp_path / "hdf" / "old.h5"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    assert run(files, tmp_path, FakeBackend(tmp_path / "hdf", produce=False)) == 3


def test_empty_data_file_is_a_failure(files, tmp_path):
    assert run(files, tmp_path, FakeBackend(tmp_path / "hdf", size=0)) == 3


def test_interrupt_returns_130_but_still_reports_the_partial_file(files, tmp_path, capsys):
    assert run(files, tmp_path, FakeBackend(tmp_path / "hdf", interrupt=True)) == 130
    assert "DAQ_OUTPUT:" in capsys.readouterr().out


# ------------------------------------------------------------------ --continuous mode
def test_continuous_and_duration_together_is_refused(files, tmp_path, capsys):
    backend = FakeBackend(tmp_path / "hdf")
    assert run(files, tmp_path, backend, extra=["--continuous"]) == 1
    assert "use either --continuous or --duration, not both" in capsys.readouterr().err
    assert backend.calls == []


def test_neither_continuous_nor_duration_is_refused(files, tmp_path, capsys):
    backend = FakeBackend(tmp_path / "hdf")
    assert run_raw(files, tmp_path, backend) == 1
    assert "either --continuous or --duration is required" in capsys.readouterr().err
    assert backend.calls == []


def test_continuous_stopped_on_request_with_valid_file_is_success_not_130(files, tmp_path, capsys):
    backend = FakeBackend(tmp_path / "hdf", interrupt=True)   # stands in for a SIGINT arriving mid-acquisition
    assert run_raw(files, tmp_path, backend, extra=["--continuous"]) == 0
    out = capsys.readouterr().out
    assert "DAQ_OUTPUT:" in out and "duration_s=continuous" in out
    assert backend.calls[-1] == ("run", 0)   # scan_timeout=0 passed through to tpx3-daq


def test_continuous_stopped_before_any_data_is_still_a_failure(files, tmp_path, capsys):
    backend = FakeBackend(tmp_path / "hdf", interrupt=True, produce=False)
    assert run_raw(files, tmp_path, backend, extra=["--continuous"]) == 3
    assert "no data file appeared" in capsys.readouterr().err


def test_continuous_does_not_run_whole_seconds_guard(files, tmp_path):
    # a --continuous run has no --duration at all, so the "0 would mean infinite" guard must not fire
    backend = FakeBackend(tmp_path / "hdf")
    assert run_raw(files, tmp_path, backend, extra=["--continuous"]) == 0


def test_help_and_argument_errors_need_no_tpx3():
    out = subprocess.run([sys.executable, "run_daq.py", "--help"], cwd=REPO, capture_output=True, text=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert out.returncode == 0 and "--equalisation" in out.stdout


def test_real_backend_import_failure_is_a_clean_exit_1(files, tmp_path, capsys):
    # conftest blocks the tpx3 imports, so this exercises the "not installed in this Python" path
    assert run(files, tmp_path, None) == 1
    assert "cannot import tpx3-daq" in capsys.readouterr().err


# ------------------------------------------------------------------ run_steps DAQ flags
def csv_file(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("0,300,1,350,2,500,60\n")
    return str(p)


def test_run_steps_daq_flags_validate_files_before_any_hv(files, tmp_path, capsys):
    backup, mask, eq = files
    base = [csv_file(tmp_path), "--channel-map", "Grid=0,Anode=1,Cathode=2", "--dry-run"]
    assert run_steps.main(base + ["--daq-backup", str(backup)]) == 2                      # all three needed
    assert run_steps.main(base + ["--daq-backup", str(backup), "--daq-mask", str(mask),
                                  "--daq-eq", str(tmp_path / "missing.h5")]) == 2
    assert run_steps.main(base + ["--daq-backup", str(backup), "--daq-mask", str(mask), "--daq-eq", str(eq),
                                  "--daq-cmd", "x.py"]) == 2
    capsys.readouterr()
    assert run_steps.main(base + ["--daq-backup", str(backup), "--daq-mask", str(mask), "--daq-eq", str(eq)]) == 0
    out = capsys.readouterr().out
    assert "run_daq.py" in out and str(backup) in out and "{duration}" in out
